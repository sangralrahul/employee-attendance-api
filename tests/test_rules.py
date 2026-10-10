"""Pure business-rule tests (R1-R5, R7). No MongoDB required:  pytest tests/test_rules.py"""
from datetime import date, datetime, timezone

import pytest

from app import main as m

DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=m.IST).astimezone(timezone.utc)


# ---- epoch ms <-> datetime -------------------------------------------------------------------
def test_ms_round_trip_truncates_to_seconds():
    dt = m.ms_to_dt(1783312500900)
    assert dt == datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc)  # .900 dropped (R1)
    assert m.dt_to_ms(dt) == 1783312500000


def test_dt_to_ms_is_exact_integer_arithmetic():
    dt = datetime(2026, 7, 6, 3, 58, 0, 123000, tzinfo=timezone.utc)
    assert m.dt_to_ms(dt) == 1783310280123
    assert m.dt_to_ms(None) is None
    naive = datetime(2026, 7, 6, 3, 58)  # naive is treated as UTC
    assert m.dt_to_ms(naive) == m.dt_to_ms(naive.replace(tzinfo=timezone.utc))


# ---- R1 attendance date ------------------------------------------------------------------------
def test_attendance_date_is_the_ist_calendar_date():
    assert m.attendance_date(ist(2026, 7, 6, 0, 10), DAY) == "2026-07-06"
    assert m.attendance_date(datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc), DAY) == "2026-07-07"  # 01:30 IST


def test_attendance_date_overnight_shift_before_shift_end_belongs_to_previous_day():
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), NIGHT) == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 1, 30), NIGHT) == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), NIGHT) == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 6, 0), NIGHT) == "2026-07-07"  # not strictly earlier


# ---- R2 late -----------------------------------------------------------------------------------
@pytest.mark.parametrize("h, mi, s, expected", [
    (9, 28, 0, 0), (9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (10, 5, 0, 35), (8, 0, 0, 0),
])
def test_late_minutes_examples_from_contract(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), DAY, "2026-07-06") == expected


def test_late_minutes_overnight_anchors_on_the_shift_start_day():
    assert m.compute_late_minutes(ist(2026, 7, 6, 22, 45), NIGHT, "2026-07-06") == 45
    assert m.compute_late_minutes(ist(2026, 7, 7, 0, 30), NIGHT, "2026-07-06") == 150


# ---- R3 overtime -------------------------------------------------------------------------------
@pytest.mark.parametrize("h, mi, s, expected", [
    (18, 35, 0, 0), (18, 59, 59, 0), (19, 0, 0, 30), (19, 10, 0, 40), (19, 10, 59, 40), (17, 0, 0, 0),
])
def test_overtime_threshold_and_floor(h, mi, s, expected):
    assert m.compute_overtime_minutes(ist(2026, 7, 6, h, mi, s), DAY, "2026-07-06") == expected


def test_overtime_overnight_shift_ends_next_day():
    assert m.compute_overtime_minutes(ist(2026, 7, 7, 6, 40), NIGHT, "2026-07-06") == 40
    assert m.compute_overtime_minutes(ist(2026, 7, 7, 6, 20), NIGHT, "2026-07-06") == 0


# ---- R4 / R5 work hours & half day --------------------------------------------------------------
def test_work_hours_half_up_not_bankers():
    pin = ist(2026, 7, 6, 9, 0)
    assert m.compute_work_hours(pin, ist(2026, 7, 6, 18, 7)) == 9.12       # 9.1166.. -> 9.12
    assert m.compute_work_hours(pin, ist(2026, 7, 6, 9, 0, 45)) == 0.01    # 0.0125 -> 0.01 (banker's gives 0.01 too)
    assert m.compute_work_hours(pin, ist(2026, 7, 6, 9, 2, 15)) == 0.04    # 0.0375 -> 0.04 (banker's gives 0.04 too)
    assert m.compute_work_hours(pin, ist(2026, 7, 6, 9, 0, 27)) == 0.01    # 0.0075 -> 0.01 (banker's gives 0.00)
    assert m.compute_work_hours(pin, ist(2026, 7, 6, 11, 40, 30)) == 2.68  # 2.675 -> 2.68 (float round() gives 2.67)


def test_derive_fields_states():
    emp = DAY
    assert m.derive_fields(emp, "2026-07-06", "LEAVE", None, None) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    open_rec = m.derive_fields(emp, "2026-07-06", "PRESENT", ist(2026, 7, 6, 10, 5), None)
    assert open_rec == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}
    closed = m.derive_fields(emp, "2026-07-06", "WFH", ist(2026, 7, 6, 9, 30), ist(2026, 7, 6, 13, 59, 59))
    assert closed["work_hours"] == 4.5 and closed["half_day"] is False   # 4.49972 -> 4.50, not < 4.50
    half = m.derive_fields(emp, "2026-07-06", "WFH", ist(2026, 7, 6, 9, 30), ist(2026, 7, 6, 13, 59, 41))
    assert half["work_hours"] == 4.49 and half["half_day"] is True


# ---- R7 working days ---------------------------------------------------------------------------
def test_working_days():
    assert m.working_days_between(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.working_days_between(date(2026, 7, 13), date(2026, 7, 31)) == 15  # mid-month joiner (Monday)
    assert m.working_days_between(date(2026, 7, 4), date(2026, 7, 5)) == 0    # a weekend
    assert m.working_days_between(date(2026, 8, 1), date(2026, 7, 31)) == 0   # joined after the month
    assert m.working_days_between(date(2026, 2, 1), date(2026, 2, 28)) == 20


def test_month_bounds():
    assert m.month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
    assert m.month_bounds("2028-02") == ("2028-02-01", "2028-02-29")
    assert m.month_bounds("2026-12") == ("2026-12-01", "2026-12-31")


def test_round_half_up_helper():
    from decimal import Decimal
    assert m.round_half_up(Decimal("0.125"), 2) == 0.13
    assert m.round_half_up(Decimal("2.675"), 2) == 2.68
    assert m.round_half_up(Decimal("66.66666"), 2) == 66.67
    assert m.round_half_up(Decimal("0.66665"), 4) == 0.6667
