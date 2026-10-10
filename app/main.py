"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this file (everything lives here by contract):

    1. Configuration & database client
    2. Time helpers           - epoch ms <-> BSON datetime, IST calendar logic
    3. Rounding helpers       - half-up rounding (R8) in Python and inside aggregation pipelines
    4. Business rules R1-R5   - attendance date, late, overtime, work hours, half day
    5. Serialisation          - MongoDB documents -> API JSON (never leaks _id)
    6. Validation helpers     - 422 errors in FastAPI's own body shape
    7. Request models         - Pydantic v2, strict integers for instants
    8. Indexes & lifespan
    9. System endpoints       - /health
   10. Employees              - POST / GET /employees
   11. Attendance             - punch-in, punch-out, list, regularize (PATCH)
   12. Analytics builders     - the aggregation pipelines, shared with /admin/explain
   13. Analytics endpoints
   14. Admin                  - /admin/explain/{endpoint}

Conventions: every instant is a tz-aware UTC datetime inside the process and an integer of epoch
milliseconds at the HTTP boundary; calendar dates / months / shift times are plain strings.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, NoReturn, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration & database client
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # .env is a convenience only: real environment variables take priority

log = logging.getLogger("attendance")
logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    log.warning("MONGO_URI / MONGO_DB not set; falling back to mongodb://localhost:27017 / attendance_db")
    MONGO_URI = MONGO_URI or "mongodb://localhost:27017"
    MONGO_DB = MONGO_DB or "attendance_db"

# tz_aware=True: every datetime read from MongoDB is an aware UTC datetime (PyMongo returns naive ones
# by default, which silently breaks arithmetic against aware values). Short server-selection timeout so
# /health answers 503 quickly instead of hanging for 30 s when MongoDB is down.
client: MongoClient = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[MONGO_DB]
employees: Collection = db["employees"]
attendance: Collection = db["attendance_logs"]

# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_DAY = timedelta(days=1)
GRACE_SECONDS = 10 * 60          # R2
OVERTIME_MIN_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
INSTANT_FIELDS = ("punch_in", "punch_out")

EPOCH_MS_MIN = 100_000_000_000    # anything below "looks like seconds"
EPOCH_MS_MAX = 4_102_444_800_000  # 2100-01-01


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Aware (or naive-as-UTC) datetime -> epoch milliseconds using exact integer arithmetic."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def ist_instant(date_str: str, hhmm: str, day_offset: int = 0) -> datetime:
    """The instant 'hhmm IST on date_str (+ day_offset days)', as an aware datetime."""
    day = date.fromisoformat(date_str) + timedelta(days=day_offset)
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=IST)


def is_overnight(emp: dict) -> bool:
    """shift_end <= shift_start means the shift crosses midnight (R1)."""
    return emp["shift_end"] <= emp["shift_start"]


# --------------------------------------------------------------------------- #
# 3. Rounding helpers (R4, R8: half-up, never banker's rounding)
# --------------------------------------------------------------------------- #
def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


ROUND_EPSILON = 1e-9


def mongo_round_half_up(expr: Any, places: int) -> dict:
    """
    Half-up rounding inside an aggregation pipeline. MongoDB's $round is half-to-even, so a tiny
    positive epsilon is added first: an exact .5 tie becomes "just above .5" and rounds up, while any
    non-tie value is far more than 1e-9 away from a tie for the magnitudes used here. Null stays null.
    """
    return {"$round": [{"$add": [expr, ROUND_EPSILON]}, places]}


# --------------------------------------------------------------------------- #
# 4. Business rules R1-R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the IST calendar date of punch_in; for overnight shifts a punch-in before shift_end belongs to
    the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= ONE_DAY
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, emp: dict, date_str: str) -> int:
    """R2: late only when strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    start = ist_instant(date_str, emp["shift_start"])
    seconds = (punch_in - start) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, emp: dict, date_str: str) -> int:
    """R3: whole minutes after shift_end (which is on the next calendar day for overnight shifts),
    counted only when at least 30."""
    end = ist_instant(date_str, emp["shift_end"], day_offset=1 if is_overnight(emp) else 0)
    seconds = (punch_out - end) // timedelta(seconds=1)
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up, computed exactly with Decimal."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_fields(emp: dict, date_str: str, status: str, punch_in: Optional[datetime],
                  punch_out: Optional[datetime]) -> dict:
    """The four derived fields for a record in its final state (see 'Who computes what' in the contract)."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"work_hours": None, "late_minutes": compute_late_minutes(punch_in, emp, date_str),
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = hours
        out["overtime_minutes"] = compute_overtime_minutes(punch_out, emp, date_str)
        out["half_day"] = Decimal(str(hours)) < HALF_DAY_BELOW_HOURS  # R5 uses the *rounded* value
    return out


def working_days_between(start: date, end: date) -> int:
    """R7: Monday-Friday count in [start, end], no holiday calendar."""
    if start > end:
        return 0
    full_weeks, rest = divmod((end - start).days + 1, 7)
    days = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=i)).weekday() < 5:
            days += 1
    return days


# --------------------------------------------------------------------------- #
# 5. Serialisation (MongoDB document -> API JSON)
# --------------------------------------------------------------------------- #


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def _serialize_change(field: str, change: dict) -> dict:
    if field in INSTANT_FIELDS:
        return {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
    return {"from": change.get("from"), "to": change.get("to")}


def serialize_record(doc: dict) -> dict:
    """Fills the gaps legacy documents may have (missing half_day / history / counters)."""
    history = [
        {
            "at": dt_to_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {k: _serialize_change(k, v) for k, v in (entry.get("changes") or {}).items()},
        }
        for entry in (doc.get("history") or [])
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 6. Validation helpers (422 in FastAPI's own body shape)
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EMP_CODE_RE = r"^EMP[0-9]{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def fail_422(msg: str, *loc: str) -> NoReturn:
    raise HTTPException(status_code=422, detail=[{"type": "value_error", "loc": list(loc), "msg": msg}])


def parse_date_param(value: Optional[str], *loc: str, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            fail_422("field required", *loc)
        return None
    if not DATE_RE.match(value):
        fail_422("must be a calendar date, YYYY-MM-DD", *loc)
    try:
        date.fromisoformat(value)
    except ValueError:
        fail_422("not a valid calendar date", *loc)
    return value


def month_bounds(month: Optional[str], *loc: str) -> tuple[str, str]:
    """'YYYY-MM' -> (first day, last day) as date strings."""
    if month is None:
        fail_422("field required", *loc)
    if not MONTH_RE.match(month):
        fail_422("must be a calendar month, YYYY-MM", *loc)
    year, mon = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


# --------------------------------------------------------------------------- #
# 7. Request models
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


# StrictInt: floats (1.7e12), numeric strings and seconds-looking values are all rejected with 422.
EpochMillis = Annotated[StrictInt, Field(ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, value: str) -> str:
        if not DATE_RE.match(value):
            raise ValueError("must be a calendar date, YYYY-MM-DD")
        date.fromisoformat(value)  # raises ValueError for e.g. 2026-02-30
        return value

    @model_validator(mode="after")
    def _shift_times_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")  # derived fields sent by a client are ignored
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def _no_explicit_nulls(cls, data: Any) -> Any:
        # "omitted means unchanged"; an explicit null is not a valid value for these fields
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} may be omitted but not null")
        return data


# --------------------------------------------------------------------------- #
# 8. Indexes & lifespan
# --------------------------------------------------------------------------- #
# name -> (collection, keys, options). Names are fixed so create_index stays idempotent across restarts.
IDX_EMP_CODE = "emp_code_unique"
IDX_EMP_DEPT_CODE = "department_emp_code"
IDX_EMP_DEPT_JOINED = "department_joined_on"
IDX_EMP_JOINED = "joined_on"
IDX_LOG_EMP_DATE = "emp_code_date_unique"
IDX_LOG_DATE_EMP = "date_desc_emp_code"
IDX_LOG_STATUS_DATE = "status_date_desc_emp_code"

INDEXES: list[tuple[Collection, str, list[tuple[str, int]], dict]] = [
    (employees, IDX_EMP_CODE, [("emp_code", ASCENDING)], {"unique": True}),
    (employees, IDX_EMP_DEPT_CODE, [("department", ASCENDING), ("emp_code", ASCENDING)], {}),
    (employees, IDX_EMP_DEPT_JOINED, [("department", ASCENDING), ("joined_on", ASCENDING)], {}),
    (employees, IDX_EMP_JOINED, [("joined_on", ASCENDING)], {}),
    (attendance, IDX_LOG_EMP_DATE, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
    (attendance, IDX_LOG_DATE_EMP, [("date", DESCENDING), ("emp_code", ASCENDING)], {}),
    (attendance, IDX_LOG_STATUS_DATE, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an index with the same name and keys already exists."""
    global _indexes_ready
    if _indexes_ready:
        return
    for coll, name, keys, options in INDEXES:
        coll.create_index(keys, name=name, **options)
    _indexes_ready = True
    log.info("all %d indexes ready on %s", len(INDEXES), MONGO_DB)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB not up yet: /health keeps retrying, startup is not blocked
        log.warning("index creation deferred, MongoDB unavailable at startup: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def _mongo_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 9. System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        return JSONResponse(status_code=503, content={"detail": f"MongoDB unavailable: {exc.__class__.__name__}"})
    try:
        ensure_indexes()  # picks up a MongoDB that was down when the process started
    except PyMongoError as exc:
        log.error("index creation failed: %s", exc)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 10. Employees
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail=f"employee {emp_code} not found")
    return emp


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # uniqueness is the unique index's job, not a find-then-insert
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"emp_code {body.emp_code} already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    hint = IDX_EMP_DEPT_CODE if department is not None else IDX_EMP_CODE
    cursor = (
        employees.find(query)
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(hint)
    )
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": employees.count_documents(query, hint=hint),
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# 11. Attendance
# --------------------------------------------------------------------------- #
def build_attendance_list(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                          status: Optional[str], page: int, page_size: int) -> tuple[dict, list, int, str]:
    """Shared by GET /attendance and /admin/explain/attendance_list: (filter, sort, skip, index hint)."""
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    # The contract orders by date desc, then emp_code asc. With an emp_code filter the second key is
    # constant, so sorting on date alone gives the identical order and lets the (emp_code, date) index
    # serve the sort by walking backwards. Each branch names the index that provides the sort for free.
    if emp_code is not None:
        sort, hint = [("date", DESCENDING)], IDX_LOG_EMP_DATE
    elif status is not None:
        sort, hint = [("date", DESCENDING), ("emp_code", ASCENDING)], IDX_LOG_STATUS_DATE
    else:
        sort, hint = [("date", DESCENDING), ("emp_code", ASCENDING)], IDX_LOG_DATE_EMP
    return query, sort, (page - 1) * page_size, hint


def validate_date_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    parse_date_param(date_from, "query", "date_from")
    parse_date_param(date_to, "query", "date_to")
    if date_from and date_to and date_from > date_to:
        fail_422("date_from must not be after date_to", "query", "date_from")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_date_range(date_from, date_to)
    query, sort, skip, hint = build_attendance_list(
        emp_code, date_from, date_to, status.value if status else None, page, page_size
    )
    cursor = attendance.find(query).sort(sort).skip(skip).limit(page_size).hint(hint)
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": attendance.count_documents(query, hint=hint),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(punched_at, emp)
    doc = {
        "emp_code": emp["emp_code"],
        "date": date_str,
        "status": body.status.value,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp, date_str),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique (emp_code, date) index decides the race: exactly one insert wins, the rest get 409.
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"{emp['emp_code']} already punched in for {date_str}")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before punched_at - independent of the calendar date,
    # which is what makes overnight shifts work. The (emp_code, date) index narrows to one employee.
    record = attendance.find_one(
        {"emp_code": emp["emp_code"], "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found at or before punched_at")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail=f"record {record['date']} is already punched out")
    punch_in_at: datetime = record["punch_in"]
    if punched_at <= punch_in_at:
        fail_422("punched_at must be after punch_in", "body", "punched_at")
    if punched_at - punch_in_at > MAX_SHIFT:
        fail_422("punched_at must be within 24 hours of punch_in", "body", "punched_at")

    derived = derive_fields(emp, record["date"], record["status"], punch_in_at, punched_at)
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # atomic: two concurrent punch-outs -> one 200, one 409
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail=f"record {record['date']} is already punched out")
    return serialize_record(updated)


DERIVED_FIELDS = ("work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeRequest, emp_code: str = Path(...), date_str: str = Path(..., alias="date")):
    parse_date_param(date_str, "path", "date")
    emp = get_employee_or_404(emp_code)
    record = attendance.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(status_code=404, detail=f"no attendance record for {emp_code} on {date_str}")

    current = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day") or False),
    }

    # --- resolve the final state of the three editable fields -----------------------------------
    new_status = body.status.value if body.status is not None else current["status"]
    if new_status not in PRESENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            fail_422(f"punch times cannot be supplied with status {new_status}", "body", "status")
        new_in, new_out = None, None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else current["punch_in"]
        if new_in is None:
            fail_422(f"status {new_status} requires a punch_in", "body", "punch_in")
        if attendance_date(new_in, emp) != date_str:
            fail_422(f"punch_in must fall on attendance date {date_str} (R1)", "body", "punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else current["punch_out"]
        if new_out is not None:
            if new_out <= new_in:
                fail_422("punch_out must be after punch_in", "body", "punch_out")
            if new_out - new_in > MAX_SHIFT:
                fail_422("punch_out must be within 24 hours of punch_in", "body", "punch_out")

    final = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
             **derive_fields(emp, date_str, new_status, new_in, new_out)}

    # --- diff: only fields that actually changed go into the history entry ------------------------
    def differs(field: str) -> bool:
        if field in INSTANT_FIELDS:
            return dt_to_ms(current[field]) != dt_to_ms(final[field])
        return current[field] != final[field]

    changes = {f: {"from": current[f], "to": final[f]} for f in final if differs(f)}
    if not changes:
        fail_422("the request changes nothing", "body")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_len = len(record.get("history") or [])
    # Optimistic concurrency: the update only applies if nobody else appended to history since we read
    # the record. The loser matches nothing and gets 409 instead of silently overwriting.
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]}},
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 12. Analytics builders (one function per endpoint; /admin/explain reuses them unchanged)
# --------------------------------------------------------------------------- #
LOGS = attendance.name
EMPLOYEES = employees.name


def _is_present(prefix: str = "$") -> dict:
    return {"$in": [f"{prefix}status", list(PRESENCE_STATUSES)]}


def _present_value(prefix: str = "$") -> dict:
    """1 for a presence record, 0.5 when it is a half day, 0 otherwise (R5, R6)."""
    return {"$cond": [_is_present(prefix), {"$cond": [{"$eq": [f"{prefix}half_day", True]}, 0.5, 1]}, 0]}


def _is_late(prefix: str = "$") -> dict:
    return {"$gt": [{"$ifNull": [f"{prefix}late_minutes", 0]}, 0]}


def _is_weekday_str(date_expr: Any) -> dict:
    """Mon-Fri test for a 'YYYY-MM-DD' string (R7)."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d"}}}, 5]}


def _month_stats_group() -> dict:
    """Per-employee totals over a set of records (employee monthly, and the inner stage of the summary)."""
    has_hours = {"$and": [_is_present(), {"$isNumber": "$work_hours"}]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [_is_weekday_str("$date"), _present_value(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [_is_late(), 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "work_hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
        "work_hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }}


def build_employee_monthly(emp_code: str, start: str, end: str) -> tuple[str, list, str]:
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        _month_stats_group(),
    ]
    return LOGS, pipeline, IDX_LOG_EMP_DATE


def build_department_summary(start: str, end: str, department: Optional[str]) -> tuple[str, list, str]:
    """Starts from employees so that people with no logs are still in the headcount (R9)."""
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    avg = {"$divide": ["$work_hours_sum", "$work_hours_count"]}
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": LOGS, "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, _month_stats_group()],
            "as": "s",
        }},
        {"$addFields": {"s": {"$first": "$s"}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$s.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$s.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_days", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": mongo_round_half_up("$present_days", 2),
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, mongo_round_half_up(avg, 2), None]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": ASCENDING}},
    ]
    return EMPLOYEES, pipeline, (IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED)


def build_late_leaderboard(start: str, end: str, limit: int, department: Optional[str]) -> tuple[str, list, str]:
    employee_lookup = [{"$project": {"_id": 0, "name": 1, "department": 1}}]
    if department is not None:  # ranking happens inside the department only
        employee_lookup.insert(0, {"$match": {"department": department}})
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": EMPLOYEES, "localField": "_id", "foreignField": "emp_code",
                     "pipeline": employee_lookup, "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist (or is outside the department)
        # $rank = standard competition ranking: equal totals share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, after ranking
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return LOGS, pipeline, IDX_LOG_DATE_EMP


def build_department_trend(department: str, start: str, end: str) -> tuple[str, list, str]:
    """
    One row per calendar day in [start, end]:
      employees of the department -> their logs in range -> per-day counts -> $densify fills the missing
      days -> per-day headcount from joined_on -> rate -> 7-row moving average with $setWindowFields.
    A sentinel zero-row for `start` is unioned in so the stream is never empty, even for a department
    that has employees but no logs in the range; $densify then fills every other day from it.
    """
    start_dt = datetime.combine(date.fromisoformat(start), time(), tzinfo=UTC)
    upper_dt = datetime.combine(date.fromisoformat(end), time(), tzinfo=UTC) + ONE_DAY  # exclusive
    rate = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        mongo_round_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
        None,
    ]}
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": LOGS, "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}},
                         {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}],
            "as": "logs",
        }},
        {"$unwind": "$logs"},
        {"$replaceWith": {"date": "$logs.date", "present": _present_value("$logs."),
                          "late": {"$cond": [_is_late("$logs."), 1, 0]}}},
        {"$unionWith": {"pipeline": [{"$documents": [{"date": start, "present": 0, "late": 0}]}]}},
        {"$group": {"_id": "$date", "present_count": {"$sum": "$present"}, "late_count": {"$sum": "$late"}}},
        {"$project": {"_id": 0, "present_count": 1, "late_count": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, upper_dt]}}},
        {"$addFields": {
            "department": department,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {  # headcount on that day: department employees whose joined_on <= day (R9)
            "from": EMPLOYEES, "localField": "department", "foreignField": "department",
            "let": {"day": "$date"},
            "pipeline": [{"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$addFields": {"attendance_rate": rate}},
        {"$setWindowFields": {
            "sortBy": {"day": ASCENDING},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": ASCENDING}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": 1, "moving_avg_7d": mongo_round_half_up("$moving_avg_7d", 4)}},
    ]
    return EMPLOYEES, pipeline, IDX_EMP_DEPT_JOINED


def run_pipeline(collection_name: str, pipeline: list, hint: str) -> list[dict]:
    return list(db[collection_name].aggregate(pipeline, hint=hint))


# --------------------------------------------------------------------------- #
# 13. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Optional[str] = Query(None, description="YYYY-MM")):
    start, end = month_bounds(month, "query", "month")
    emp = get_employee_or_404(emp_code)
    rows = run_pipeline(*build_employee_monthly(emp_code, start, end))
    stats = rows[0] if rows else {}

    working_days = working_days_between(max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(start)),
                                        date.fromisoformat(end))
    present_days = round_half_up(Decimal(str(stats.get("present_days", 0))), 2)
    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Optional[str] = Query(None, description="YYYY-MM"), department: Optional[str] = None):
    start, end = month_bounds(month, "query", "month")
    items = run_pipeline(*build_department_summary(start, end, department))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Optional[str] = Query(None, description="YYYY-MM"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month, "query", "month")
    items = run_pipeline(*build_late_leaderboard(start, end, limit, department))
    return {"month": month, "items": items}


def validate_trend_range(start: Optional[str], end: Optional[str]) -> tuple[str, str]:
    parse_date_param(start, "query", "from", required=True)
    parse_date_param(end, "query", "to", required=True)
    if end < start:
        fail_422("to must not be before from", "query", "to")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 > MAX_TREND_DAYS:
        fail_422(f"range must not exceed {MAX_TREND_DAYS} days", "query", "to")
    return start, end


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Optional[str] = Query(None, alias="from"),
    end: Optional[str] = Query(None, alias="to"),
):
    start, end = validate_trend_range(start, end)
    if employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail=f"department {department} not found")
    items = run_pipeline(*build_department_trend(department, start, end))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 14. Admin: explain
# --------------------------------------------------------------------------- #
EXPLAINABLE = ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend")


def explain_command(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw))  # raw explain, made JSON-safe (ObjectId, dates, Long)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    start: Optional[str] = Query(None, alias="from"),
    end: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAINABLE:
        fail_422(f"endpoint must be one of {', '.join(EXPLAINABLE)}", "path", "endpoint")

    if endpoint == "attendance_list":
        validate_date_range(date_from, date_to)
        query, sort, skip, hint = build_attendance_list(
            emp_code, date_from, date_to, status.value if status else None, page, page_size
        )
        command = {"find": LOGS, "filter": query, "sort": dict(sort), "skip": skip, "limit": page_size, "hint": hint}
        return {"endpoint": endpoint, "collection": LOGS, "explain": explain_command(command)}

    if endpoint == "employee_monthly":
        if emp_code is None:
            fail_422("field required", "query", "emp_code")
        s, e = month_bounds(month, "query", "month")
        coll, pipeline, hint = build_employee_monthly(emp_code, s, e)
    elif endpoint == "department_summary":
        s, e = month_bounds(month, "query", "month")
        coll, pipeline, hint = build_department_summary(s, e, department)
    elif endpoint == "late_leaderboard":
        s, e = month_bounds(month, "query", "month")
        coll, pipeline, hint = build_late_leaderboard(s, e, limit, department)
    else:  # department_trend
        if department is None:
            fail_422("field required", "query", "department")
        s, e = validate_trend_range(start, end)
        coll, pipeline, hint = build_department_trend(department, s, e)

    command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    return {"endpoint": endpoint, "collection": coll, "explain": explain_command(command)}
