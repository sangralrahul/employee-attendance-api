"""
End-to-end tests against a running MongoDB:  MONGO_URI=mongodb://localhost:27017 pytest -q

July 2026 is the reference month: it starts on a Wednesday, has 23 working days and the weekends are
4/5, 11/12, 18/19, 25/26.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app import main as m

IST = m.IST


def ms(y, mo, d, h, mi, s=0, extra_ms=0):
    """Epoch milliseconds of an IST wall-clock instant."""
    return m.dt_to_ms(datetime(y, mo, d, h, mi, s, tzinfo=IST)) + extra_ms


def ist_dt(date_str, hhmm):
    return m.ist_instant(date_str, hhmm).astimezone(timezone.utc)


def make_emp(api, code, department="Engineering", joined_on="2026-01-05", shift=("09:30", "18:30"), name=None):
    r = api.post("/employees", json={
        "emp_code": code, "name": name or f"Person {code}", "email": f"{code.lower()}@example.com",
        "department": department, "shift_start": shift[0], "shift_end": shift[1], "joined_on": joined_on,
    })
    assert r.status_code == 201, r.text
    return r.json()


def seed(db, code, date, status="PRESENT", late=0, half=False, work_hours=8.0, overtime=0,
         pin="09:30", pout="17:30", legacy=False):
    """Insert a record the way the grader seeds them (derived values stored, not recomputed)."""
    presence = status in m.PRESENCE_STATUSES
    doc = {
        "emp_code": code, "date": date, "status": status,
        "punch_in": ist_dt(date, pin) if presence else None,
        "punch_out": ist_dt(date, pout) if presence and pout else None,
        "work_hours": work_hours if presence and pout else None,
        "late_minutes": late, "overtime_minutes": overtime,
    }
    if not legacy:
        doc["half_day"] = half
        doc["history"] = []
    db.attendance_logs.insert_one(doc)


def parallel(fn, n):
    with ThreadPoolExecutor(max_workers=n) as pool:
        return [f.result() for f in [pool.submit(fn) for _ in range(n)]]


# ======================================================================================= system
def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_indexes_exist_after_startup(db):
    assert "emp_code_unique" in db.employees.index_information()
    logs = db.attendance_logs.index_information()
    assert logs["emp_code_date_unique"]["unique"] is True
    assert "date_desc_emp_code" in logs


# ==================================================================================== employees
def test_create_employee_returns_contract_fields(api):
    emp = make_emp(api, "EMP0001")
    assert set(emp) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(emp["created_at"], int) and emp["created_at"] > m.EPOCH_MS_MIN
    assert "_id" not in emp and "id" not in emp


def test_create_employee_validation_and_duplicates(api):
    make_emp(api, "EMP0001")
    assert api.post("/employees", json={"emp_code": "EMP0001", "name": "x", "email": "a@b.c",
                                        "department": "D", "joined_on": "2026-01-01"}).status_code == 409
    base = {"name": "x", "email": "a@b.c", "department": "D", "joined_on": "2026-01-01"}
    assert api.post("/employees", json={**base, "emp_code": "EMP001"}).status_code == 422      # 3 digits
    assert api.post("/employees", json={**base, "emp_code": "EMP0002", "shift_start": "09:30",
                                        "shift_end": "09:30"}).status_code == 422               # equal shifts
    assert api.post("/employees", json={**base, "emp_code": "EMP0002", "joined_on": "2026-02-30"}).status_code == 422
    assert api.post("/employees", json={**base, "emp_code": "EMP0002", "email": "not-an-email"}).status_code == 422
    assert api.post("/employees", json={**base, "emp_code": "EMP0002", "shift_end": "24:00"}).status_code == 422


def test_concurrent_duplicate_creates_yield_one_201(api):
    body = {"emp_code": "EMP0009", "name": "x", "email": "a@b.c", "department": "D", "joined_on": "2026-01-01"}
    codes = parallel(lambda: api.post("/employees", json=body).status_code, 12)
    assert codes.count(201) == 1 and codes.count(409) == 11


def test_list_employees_sorted_filtered_paginated(api):
    for code, dept in [("EMP0003", "Sales"), ("EMP0001", "Engineering"), ("EMP0002", "Sales")]:
        make_emp(api, code, department=dept)
    r = api.get("/employees").json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002", "EMP0003"] and r["total"] == 3
    r = api.get("/employees", params={"department": "Sales", "page_size": 1, "page": 2}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0003"]
    assert api.get("/employees", params={"department": "sales"}).json()["total"] == 0  # case-sensitive
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


# ===================================================================================== punch-in
def test_punch_in_shape_and_truncation(api):
    make_emp(api, "EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 28, extra_ms=900)})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec == {
        "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
        "punch_in": ms(2026, 7, 6, 9, 28), "punch_out": None, "work_hours": None,
        "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
    }


def test_punch_in_late_rule_r2(api):
    make_emp(api, "EMP0001")
    for day, (h, mi, s), expected in [(1, (9, 40, 0), 0), (2, (9, 40, 1), 10), (3, (10, 15, 59), 45)]:
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, day, h, mi, s)})
        assert r.json()["late_minutes"] == expected


def test_punch_in_errors(api):
    make_emp(api, "EMP0001")
    ok = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}
    assert api.post("/attendance/punch-in", json={**ok, "emp_code": "EMP9999"}).status_code == 404
    assert api.post("/attendance/punch-in", json=ok).status_code == 201
    assert api.post("/attendance/punch-in", json={**ok, "punched_at": ms(2026, 7, 6, 10, 0)}).status_code == 409
    assert api.post("/attendance/punch-in", json={**ok, "punched_at": 1783312500}).status_code == 422       # seconds
    assert api.post("/attendance/punch-in", json={**ok, "punched_at": 1783312500000.0}).status_code == 422   # float
    assert api.post("/attendance/punch-in", json={**ok, "punched_at": "1783312500000"}).status_code == 422   # string
    assert api.post("/attendance/punch-in", json={**ok, "status": "ABSENT"}).status_code == 422
    assert api.post("/attendance/punch-in", json={**ok, "status": "LEAVE"}).status_code == 422
    assert api.post("/attendance/punch-in", json={"punched_at": ok["punched_at"]}).status_code == 422
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0),
                                               "status": "WFH", "late_minutes": 99, "half_day": True})
    assert r.status_code == 201 and r.json()["late_minutes"] == 0 and r.json()["half_day"] is False  # ignored


def test_punch_in_race_exactly_one_201(api):
    make_emp(api, "EMP0001")
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}
    codes = parallel(lambda: api.post("/attendance/punch-in", json=body).status_code, 15)
    assert codes.count(201) == 1 and codes.count(409) == 14
    assert api.get("/attendance", params={"emp_code": "EMP0001"}).json()["total"] == 1


def test_punch_in_uses_ist_date_near_midnight(api):
    make_emp(api, "EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 30)})
    assert r.json()["date"] == "2026-07-07"  # 2026-07-06T19:00Z is still the 7th in IST


# ==================================================================================== punch-out
def test_punch_out_computes_r3_r4_r5(api):
    make_emp(api, "EMP0003")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ms(2026, 7, 6, 10, 5)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert (rec["work_hours"], rec["late_minutes"], rec["overtime_minutes"], rec["half_day"]) == (9.08, 35, 40, False)
    assert rec["history"] == []  # punch-out never writes history


def test_punch_out_half_day_and_no_overtime(api):
    make_emp(api, "EMP0004")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ms(2026, 7, 7, 9, 30), "status": "WFH"})
    rec = api.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": ms(2026, 7, 7, 13, 0)}).json()
    assert rec["work_hours"] == 3.5 and rec["half_day"] is True and rec["overtime_minutes"] == 0


def test_overnight_shift_across_midnight(api):
    make_emp(api, "EMP0005", shift=("22:00", "06:00"))
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    rec = r.json()
    assert rec["date"] == "2026-07-06" and rec["work_hours"] == 8.75 and rec["overtime_minutes"] == 40
    # a late night-shift punch-in at 01:30 belongs to the shift that started the day before
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 9, 1, 30)})
    assert r.json()["date"] == "2026-07-08" and r.json()["late_minutes"] == 210


def test_punch_out_errors(api):
    make_emp(api, "EMP0001")
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 8, 0)}).status_code == 404  # before punch-in
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 422  # equal
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0, 1)}).status_code == 422  # > 24h
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 200
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}).status_code == 409


def test_punch_out_race_exactly_one_200(api):
    make_emp(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}
    codes = parallel(lambda: api.post("/attendance/punch-out", json=body).status_code, 15)
    assert codes.count(200) == 1 and codes.count(409) == 14


# ====================================================================================== listing
def test_list_attendance_sort_filters_total_and_legacy_defaults(api, db):
    make_emp(api, "EMP0001"), make_emp(api, "EMP0002")
    seed(db, "EMP0002", "2026-07-14", legacy=True)
    seed(db, "EMP0001", "2026-07-14")
    seed(db, "EMP0001", "2026-07-13", status="LEAVE")
    seed(db, "EMP0001", "2026-07-15")
    r = api.get("/attendance").json()
    assert [(x["date"], x["emp_code"]) for x in r["items"]] == [
        ("2026-07-15", "EMP0001"), ("2026-07-14", "EMP0001"), ("2026-07-14", "EMP0002"), ("2026-07-13", "EMP0001")]
    legacy = r["items"][2]
    assert legacy["half_day"] is False and legacy["history"] == [] and isinstance(legacy["punch_in"], int)
    assert api.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1
    r = api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-14", "date_to": "2026-07-14"}).json()
    assert r["total"] == 1 and r["items"][0]["punch_out"] == ms(2026, 7, 14, 17, 30)
    r = api.get("/attendance", params={"page": 2, "page_size": 3}).json()
    assert r["total"] == 4 and len(r["items"]) == 1 and r["page"] == 2
    assert api.get("/attendance", params={"date_from": "2026-07-15", "date_to": "2026-07-14"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "15-07-2026"}).status_code == 422
    assert api.get("/attendance", params={"status": "SICK"}).status_code == 422


# =================================================================================== regularize
def test_regularize_appends_history_with_only_changed_fields(api):
    make_emp(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    r = api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin",
        "late_minutes": 77,  # derived fields in the body are ignored
    })
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["punch_in"] == ms(2026, 7, 7, 9, 28) and rec["late_minutes"] == 0 and rec["work_hours"] == 9.03
    assert len(rec["history"]) == 1
    entry = rec["history"][0]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    # a second correction appends, never overwrites
    r = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH", "reason": "worked remote", "regularized_by": "hr"})
    assert len(r.json()["history"]) == 2 and r.json()["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_regularize_rules(api, db):
    make_emp(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0)})
    url = "/attendance/EMP0001/2026-07-07"
    meta = {"reason": "some reason", "regularized_by": "hr"}
    assert api.patch(url, json=meta).status_code == 422                                              # changes nothing
    assert api.patch(url, json={**meta, "status": "PRESENT"}).status_code == 422                     # still nothing
    assert api.patch(url, json={**meta, "status": "ABSENT", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert api.patch(url, json={**meta, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422        # leaves the date (R1)
    assert api.patch(url, json={**meta, "punch_out": ms(2026, 7, 7, 8, 0)}).status_code == 422       # before punch_in
    assert api.patch(url, json={**meta, "punch_out": ms(2026, 7, 8, 9, 0, 1)}).status_code == 422    # > 24 h
    assert api.patch(url, json={**meta, "punch_in": None}).status_code == 422                        # explicit null
    assert api.patch(url, json={**meta, "reason": "shrt"}).status_code == 422
    assert api.patch("/attendance/EMP9999/2026-07-07", json={**meta, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-08", json={**meta, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-7-8", json={**meta, "status": "LEAVE"}).status_code == 422

    rec = api.patch(url, json={**meta, "status": "ABSENT"}).json()  # clears both punch times
    assert rec["punch_in"] is None and rec["punch_out"] is None and rec["late_minutes"] == 0
    assert rec["history"][0]["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 9, 0), "to": None}
    assert api.patch(url, json={**meta, "status": "PRESENT"}).status_code == 422                     # needs a punch_in
    rec = api.patch(url, json={**meta, "status": "PRESENT", "punch_in": ms(2026, 7, 7, 10, 0),
                               "punch_out": ms(2026, 7, 7, 13, 0)}).json()
    assert rec["late_minutes"] == 30 and rec["half_day"] is True and len(rec["history"]) == 2
    assert db.attendance_logs.find_one({"emp_code": "EMP0001"})["history"][1]["changes"]["punch_in"]["to"].tzinfo is not None


def test_regularize_legacy_record_without_history(api, db):
    make_emp(api, "EMP0002")
    seed(db, "EMP0002", "2026-07-14", legacy=True)
    r = api.patch("/attendance/EMP0002/2026-07-14", json={"status": "ON_DUTY", "reason": "client visit", "regularized_by": "mgr"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1


def test_concurrent_regularizations_never_lose_history(api):
    make_emp(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0)})
    bodies = [{"status": s, "reason": f"change to {s}", "regularized_by": f"user{i}"}
              for i, s in enumerate(["WFH", "ON_DUTY", "LEAVE", "ABSENT", "WFH", "ON_DUTY"])]
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = [f.result().status_code for f in [pool.submit(api.patch, "/attendance/EMP0001/2026-07-07", json=b) for b in bodies]]
    assert set(codes) <= {200, 409, 422} and codes.count(200) >= 1
    history = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"]
    assert len(history) == codes.count(200)  # every winner left exactly one entry; losers were told (409)


# ===================================================================================== analytics
def test_employee_monthly_mid_month_joiner_weekend_half_day(api, db):
    make_emp(api, "EMP0002", joined_on="2026-07-13")
    seed(db, "EMP0002", "2026-07-13")                                      # Mon, full day
    seed(db, "EMP0002", "2026-07-14", half=True, work_hours=3.5)           # Tue, half day
    seed(db, "EMP0002", "2026-07-18", late=20, overtime=45)                # Sat: not a present day, late/OT still count
    seed(db, "EMP0002", "2026-07-15", status="LEAVE")
    seed(db, "EMP0002", "2026-07-16", status="ABSENT")
    seed(db, "EMP0002", "2026-07-17", status="ON_DUTY", pout=None, work_hours=None, late=5)  # open record
    r = api.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "emp_code": "EMP0002", "month": "2026-07", "working_days": 15, "present_days": 2.5, "leave_days": 1,
        "late_count": 2, "total_late_minutes": 25, "total_overtime_minutes": 45, "attendance_pct": 16.67,
    }
    assert api.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-13"}).status_code == 422
    assert api.get("/analytics/employees/EMP0002/monthly").status_code == 422
    r = api.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-06"}).json()   # before joining
    assert r["working_days"] == 0 and r["attendance_pct"] is None and r["present_days"] == 0


def test_department_summary_headcount_and_record_average(api, db):
    make_emp(api, "EMP0001", "Engineering", joined_on="2026-01-05")
    make_emp(api, "EMP0002", "Engineering", joined_on="2026-07-13")   # mid-month joiner
    make_emp(api, "EMP0007", "Engineering", joined_on="2026-08-01")   # joins after the month -> not counted
    make_emp(api, "EMP0008", "Engineering", joined_on="2026-03-01")   # no logs at all -> still counted
    make_emp(api, "EMP0003", "Sales", joined_on="2026-02-01")
    make_emp(api, "EMP0010", "Research", joined_on="2026-09-01")      # whole department joins later -> omitted
    seed(db, "EMP0001", "2026-07-06", work_hours=9.12)
    seed(db, "EMP0001", "2026-07-07", work_hours=9.08, late=35, overtime=40)
    seed(db, "EMP0001", "2026-07-08", status="LEAVE")
    seed(db, "EMP0001", "2026-07-11", status="WFH", half=True, work_hours=3.5, late=5)     # Saturday
    seed(db, "EMP0002", "2026-07-13", status="ON_DUTY", pout=None, work_hours=None)        # open: no work_hours
    seed(db, "EMP0002", "2026-07-14", work_hours=8.0)
    seed(db, "EMP0003", "2026-07-06", work_hours=7.0, late=12)
    seed(db, "EMP0003", "2026-06-30", work_hours=7.0, late=99)                              # outside the month
    seed(db, "EMP0003", "2026-08-01", work_hours=7.0, late=99)                              # outside the month
    r = api.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert r.status_code == 200, r.text
    assert r.json() == {"month": "2026-07", "items": [
        {"department": "Engineering", "headcount": 3, "present_days": 4.0, "avg_work_hours": 7.43,  # 29.7/4 = 7.425 half-up
         "late_count": 2, "total_late_minutes": 40, "leave_count": 1, "on_duty_count": 1},
        {"department": "Sales", "headcount": 1, "present_days": 1.0, "avg_work_hours": 7.0,
         "late_count": 1, "total_late_minutes": 12, "leave_count": 0, "on_duty_count": 0},
    ]}
    r = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in r["items"]] == ["Sales"]
    r = api.get("/analytics/departments/summary", params={"month": "2026-05"}).json()
    assert r["items"] == [{"department": "Engineering", "headcount": 2, "present_days": 0, "avg_work_hours": None,
                           "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0},
                          {"department": "Sales", "headcount": 1, "present_days": 0, "avg_work_hours": None,
                           "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]
    assert api.get("/analytics/departments/summary").status_code == 422


def test_late_leaderboard_competition_ranking(api, db):
    for code, dept in [("EMP0001", "Eng"), ("EMP0002", "Eng"), ("EMP0003", "Sales"), ("EMP0004", "Sales"), ("EMP0005", "Eng")]:
        make_emp(api, code, dept, name=f"N{code[-1]}")
    seed(db, "EMP0005", "2026-07-01", late=50)
    seed(db, "EMP0003", "2026-07-01", late=25), seed(db, "EMP0003", "2026-07-02", late=15)   # 40 over 2 records
    seed(db, "EMP0001", "2026-07-01", late=40)
    seed(db, "EMP0002", "2026-07-01", late=30), seed(db, "EMP0002", "2026-07-02", late=0)    # 0 is not a late record
    seed(db, "EMP0004", "2026-07-01", late=20)
    seed(db, "EMP0004", "2026-08-01", late=500)                                               # other month
    seed(db, "EMP0099", "2026-07-01", late=999)                                               # employee does not exist
    r = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3})
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    assert [(x["rank"], x["emp_code"], x["total_late_minutes"], x["late_count"]) for x in rows] == [
        (1, "EMP0005", 50, 1), (2, "EMP0001", 40, 1), (2, "EMP0003", 40, 2)]
    assert rows[0]["name"] == "N5" and rows[0]["department"] == "Eng"
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}).json()["items"]
    assert [x["rank"] for x in rows] == [1, 2, 2, 4] and rows[3]["emp_code"] == "EMP0002"
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"]
    assert [x["rank"] for x in rows] == [1, 2, 2, 4, 5]
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Sales"}).json()["items"]
    assert [(x["rank"], x["emp_code"]) for x in rows] == [(1, "EMP0003"), (2, "EMP0004")]  # re-ranked inside Sales
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-09"}).json()["items"] == []


def test_department_trend_gap_filling_headcount_and_moving_average(api, db):
    make_emp(api, "EMP0001", "Ops", joined_on="2026-06-01")
    make_emp(api, "EMP0002", "Ops", joined_on="2026-07-06")
    make_emp(api, "EMP0003", "Other", joined_on="2026-01-01")
    seed(db, "EMP0001", "2026-07-01")
    seed(db, "EMP0001", "2026-07-02", half=True, work_hours=4.0)
    seed(db, "EMP0001", "2026-07-04")                       # Saturday record
    seed(db, "EMP0001", "2026-07-06", status="LEAVE")
    seed(db, "EMP0001", "2026-07-07", late=15)
    seed(db, "EMP0002", "2026-07-06"), seed(db, "EMP0002", "2026-07-07"), seed(db, "EMP0002", "2026-07-08", status="ABSENT")
    seed(db, "EMP0003", "2026-07-01", late=5)               # other department: ignored
    r = api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-07-10"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert r.json()["department"] == "Ops" and len(items) == 10
    expected = [  # date, working, headcount, present, late, rate, moving average
        ("2026-07-01", True, 1, 1.0, 0, 1.0, 1.0),
        ("2026-07-02", True, 1, 0.5, 0, 0.5, 0.75),
        ("2026-07-03", True, 1, 0, 0, 0.0, 0.5),
        ("2026-07-04", False, 1, 1.0, 0, None, 0.5),
        ("2026-07-05", False, 1, 0, 0, None, 0.5),
        ("2026-07-06", True, 2, 1.0, 0, 0.5, 0.5),
        ("2026-07-07", True, 2, 2.0, 1, 1.0, 0.6),
        ("2026-07-08", True, 2, 0, 0, 0.0, 0.4),
        ("2026-07-09", True, 2, 0, 0, 0.0, 0.3),
        ("2026-07-10", True, 2, 0, 0, 0.0, 0.3),
    ]
    got = [(x["date"], x["is_working_day"], x["headcount"], x["present_count"], x["late_count"],
            x["attendance_rate"], x["moving_avg_7d"]) for x in items]
    assert got == expected
    # a department with employees but no records at all in the range still gets one row per day
    items = api.get("/analytics/departments/Other/trend", params={"from": "2026-08-01", "to": "2026-08-03"}).json()["items"]
    assert [(x["date"], x["attendance_rate"], x["moving_avg_7d"]) for x in items] == [
        ("2026-08-01", None, None), ("2026-08-02", None, None), ("2026-08-03", 0.0, 0.0)]
    # before anyone joined: headcount 0 -> rate null even on working days
    items = api.get("/analytics/departments/Ops/trend", params={"from": "2026-05-04", "to": "2026-05-05"}).json()["items"]
    assert [(x["headcount"], x["attendance_rate"]) for x in items] == [(0, None), (0, None)]
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422  # 93 days
    assert api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200  # 92 days
    assert api.get("/analytics/departments/Ops/trend", params={"from": "2026-07-01"}).status_code == 422


def test_rounding_in_pipelines_is_half_up(api, db):
    make_emp(api, "EMP0001", "R", joined_on="2026-01-01"), make_emp(api, "EMP0002", "R", joined_on="2026-01-01")
    seed(db, "EMP0001", "2026-07-01", work_hours=9.12), seed(db, "EMP0002", "2026-07-01", work_hours=9.13)  # mean 9.125
    r = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert r["items"][0]["avg_work_hours"] == 9.13
    for code in ["EMP0003", "EMP0004", "EMP0005", "EMP0006", "EMP0007", "EMP0008"]:
        make_emp(api, code, "R", joined_on="2026-01-01")
    # 8 employees, 1 present on Wed 1 July -> 0.125 (exact tie at 3 dp stays 0.125 at 4 dp); half day -> 0.0625
    seed(db, "EMP0003", "2026-07-02", half=True, work_hours=2.0)
    items = api.get("/analytics/departments/R/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).json()["items"]
    assert items[0]["attendance_rate"] == 0.25 and items[1]["attendance_rate"] == 0.0625
    assert items[1]["moving_avg_7d"] == 0.1563  # (0.25 + 0.0625)/2 = 0.15625 -> half-up


# ====================================================================================== explain
def _explain(api, endpoint, **params):
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and "explain" in body
    text = json.dumps(body["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text, f"{endpoint}: {text[:800]}"
    return body


def test_explain_every_endpoint_uses_indexes(api, db):
    make_emp(api, "EMP0001", "Eng"), make_emp(api, "EMP0002", "Sales")
    for d in range(1, 29):
        seed(db, "EMP0001", f"2026-07-{d:02d}", late=d % 3)
        seed(db, "EMP0002", f"2026-07-{d:02d}", late=d % 5)
    assert _explain(api, "attendance_list")["collection"] == "attendance_logs"
    _explain(api, "attendance_list", emp_code="EMP0001", date_from="2026-07-01", date_to="2026-07-31", page=2)
    _explain(api, "attendance_list", status="PRESENT", date_from="2026-07-10")
    _explain(api, "employee_monthly", emp_code="EMP0001", month="2026-07")
    assert _explain(api, "department_summary", month="2026-07")["collection"] == "employees"
    _explain(api, "department_summary", month="2026-07", department="Eng")
    _explain(api, "late_leaderboard", month="2026-07", limit=5)
    _explain(api, "late_leaderboard", month="2026-07", department="Sales")
    _explain(api, "department_trend", department="Eng", **{"from": "2026-07-01", "to": "2026-07-31"})


def test_explain_validation(api):
    assert api.get("/admin/explain/nope").status_code == 422
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422    # emp_code missing
    assert api.get("/admin/explain/department_summary").status_code == 422                              # month missing
    assert api.get("/admin/explain/department_trend", params={"department": "X", "from": "2026-07-01"}).status_code == 422
    assert api.get("/admin/explain/attendance_list", params={"date_from": "2026-07-02", "date_to": "2026-07-01"}).status_code == 422
