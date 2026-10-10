"""
Generate a large, deliberately awkward dataset for local testing (NOT a data dump; nothing is committed).

    python scripts/generate_dataset.py --employees 400 --from 2026-01-01 --to 2026-12-31

With the defaults this writes ~100,000 attendance_logs covering: overnight shifts, mid-year joiners,
employees with no logs, weekend records, half days, open records, ABSENT / LEAVE days, legacy documents
without half_day / history, lots of late-minute ties, and a few orphan logs with no employee.
Reads MONGO_URI / MONGO_DB from the environment (or .env). Drops both collections first.
"""
import argparse
import os
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import IST, derive_fields, ist_instant  # noqa: E402  (same rule code as the API)

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "Operations", "HR", "Marketing", "Research"]
SHIFTS = [("09:30", "18:30")] * 7 + [("08:00", "17:00")] * 2 + [("22:00", "06:00")]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--employees", type=int, default=400)
    parser.add_argument("--from", dest="start", default="2026-01-01")
    parser.add_argument("--to", dest="end", default="2026-12-31")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    random.seed(args.seed)

    load_dotenv(override=False)
    db = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[
        os.environ.get("MONGO_DB", "attendance_db")]
    db.employees.drop()
    db.attendance_logs.drop()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    employees = []
    for i in range(1, args.employees + 1):
        shift = random.choice(SHIFTS)
        joined = start + timedelta(days=random.choice([0] * 8 + [random.randint(1, (end - start).days)]))
        employees.append({
            "emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "email": f"emp{i}@example.com",
            "department": random.choice(DEPARTMENTS), "shift_start": shift[0], "shift_end": shift[1],
            "joined_on": joined.isoformat(), "created_at": ist_instant(joined.isoformat(), "09:00"),
        })
    db.employees.insert_many(employees)

    batch, total = [], 0
    for emp in employees:
        if random.random() < 0.03:  # ~3% have no logs at all
            continue
        day = max(start, date.fromisoformat(emp["joined_on"]))
        while day <= end:
            weekend = day.weekday() >= 5
            if weekend and random.random() > 0.08:
                day += timedelta(days=1)
                continue
            roll = random.random()
            d = day.isoformat()
            if roll < 0.06:
                doc = {"status": "LEAVE", "punch_in": None, "punch_out": None}
            elif roll < 0.10:
                doc = {"status": "ABSENT", "punch_in": None, "punch_out": None}
            else:
                status = random.choices(["PRESENT", "WFH", "ON_DUTY"], weights=[80, 15, 5])[0]
                # late minutes clustered on round numbers so the leaderboard has plenty of ties
                late = random.choice([0, 0, 0, 0, 5, 12, 15, 20, 20, 30, 45, 60])
                punch_in = ist_instant(d, emp["shift_start"]) + timedelta(minutes=late, seconds=random.randint(0, 59))
                if random.random() < 0.05:
                    punch_out = None  # still open
                else:
                    hours = random.choice([3.5, 4.4, 4.5, 8, 8.5, 9, 9.25, 9.75, 10.5])
                    punch_out = punch_in + timedelta(hours=hours, seconds=random.randint(0, 59))
                doc = {"status": status, "punch_in": punch_in.astimezone(IST), "punch_out": punch_out}
            doc.update(derive_fields(emp, d, doc["status"], doc["punch_in"], doc["punch_out"]))
            record = {"emp_code": emp["emp_code"], "date": d, **doc}
            if random.random() < 0.05:   # legacy documents
                record.pop("half_day"), record.pop("history", None)
            else:
                record["history"] = []
            batch.append(record)
            if len(batch) >= 5000:
                db.attendance_logs.insert_many(batch)
                total += len(batch)
                batch = []
            day += timedelta(days=1)
    for i in range(5):  # orphan logs: employee does not exist
        batch.append({"emp_code": f"EMP99{i:02d}", "date": "2026-07-01", "status": "PRESENT",
                      "punch_in": ist_instant("2026-07-01", "10:30"), "punch_out": None, "work_hours": None,
                      "late_minutes": 60, "overtime_minutes": 0, "half_day": False, "history": []})
    if batch:
        db.attendance_logs.insert_many(batch)
        total += len(batch)
    print(f"{len(employees)} employees, {total} attendance_logs written to {db.name}")


if __name__ == "__main__":
    main()
