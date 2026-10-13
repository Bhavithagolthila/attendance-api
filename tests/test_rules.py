"""Pure-logic tests for the business rules. No database needed:  pytest tests/test_rules.py"""
from datetime import date, datetime, timezone

import pytest

from app import main as m

UTC = timezone.utc


def ist(y, mo, d, h, mi, s=0):
    """Build an instant from an IST wall-clock time."""
    return datetime(y, mo, d, h, mi, s, tzinfo=m.IST)


# ---- R2: late ------------------------------------------------------------- #
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 30, 0, 0),
    (9, 40, 0, 0),      # exactly 10:00 after the start is NOT late
    (9, 40, 1, 10),     # one second more: late, 10 whole minutes since shift start
    (10, 15, 59, 45),   # floor, counted from shift start (not from the end of grace)
    (9, 0, 0, 0),       # early
])
def test_late_minutes(h, mi, s, expected):
    p = ist(2026, 7, 6, h, mi, s)
    assert m.compute_late_minutes(p, date(2026, 7, 6), "09:30", "18:30") == expected


# ---- R3: overtime --------------------------------------------------------- #
@pytest.mark.parametrize("h,mi,expected", [
    (18, 30, 0), (18, 59, 0), (19, 0, 30), (19, 10, 40), (17, 0, 0),
])
def test_overtime(h, mi, expected):
    assert m.compute_overtime(ist(2026, 7, 6, h, mi), date(2026, 7, 6), "09:30", "18:30") == expected


def test_overtime_overnight_shift_ends_next_day():
    # shift 22:00-06:00 that started on the 6th ends on the 7th at 06:00
    out = ist(2026, 7, 7, 6, 40)
    assert m.compute_overtime(out, date(2026, 7, 6), "22:00", "06:00") == 40


# ---- R4 / R5: work hours and half day -------------------------------------- #
def test_work_hours_half_up_not_bankers():
    # 4h 30m 18s = 4.505 h  -> half-up gives 4.51 (banker's/float rounding gives 4.5)
    p_in = ist(2026, 7, 6, 9, 0, 0)
    p_out = ist(2026, 7, 6, 13, 30, 18)
    assert m.compute_work_hours(p_in, p_out) == 4.51


def test_work_hours_sample_values():
    assert m.compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12
    assert m.compute_work_hours(ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0)) == 3.5


def test_half_day_uses_rounded_hours():
    assert m.compute_half_day(4.49) is True
    assert m.compute_half_day(4.50) is False
    # 4h 29m 40s = 4.4944 h -> rounds to 4.49 -> half day
    wh = m.compute_work_hours(ist(2026, 7, 6, 9, 0, 0), ist(2026, 7, 6, 13, 29, 40))
    assert wh == 4.49 and m.compute_half_day(wh)
    # 4h 29m 50s = 4.4972 h -> rounds to 4.50 -> NOT a half day
    wh = m.compute_work_hours(ist(2026, 7, 6, 9, 0, 0), ist(2026, 7, 6, 13, 29, 50))
    assert wh == 4.5 and not m.compute_half_day(wh)


# ---- R1: attendance date --------------------------------------------------- #
def test_day_shift_date_is_ist_date_not_utc_date():
    # 00:30 IST on the 7th is still the 6th in UTC; the record belongs to the 7th.
    p = datetime(2026, 7, 6, 19, 0, tzinfo=UTC)  # = 2026-07-07 00:30 IST
    assert m.attendance_date(p, "09:30", "18:30") == date(2026, 7, 7)


def test_overnight_punch_in_before_shift_end_belongs_to_previous_day():
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 7, 5, 30), "22:00", "06:00") == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == date(2026, 7, 7)  # not *earlier* than end


def test_overnight_sample_record():
    # Sample document 7: in 21:55 IST, out 06:40 IST next day -> 8.75 h, 40 min overtime
    p_in, p_out = ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)
    assert m.compute_work_hours(p_in, p_out) == 8.75
    assert m.compute_overtime(p_out, date(2026, 7, 6), "22:00", "06:00") == 40
    assert m.compute_late_minutes(p_in, date(2026, 7, 6), "22:00", "06:00") == 0


# ---- conversions and rounding ---------------------------------------------- #
def test_ms_round_trip_and_truncation():
    ms = 1783312500123
    assert m.to_ms(m.from_ms(ms)) == 1783312500000  # truncated to whole seconds
    assert m.from_ms(ms).tzinfo is not None
    naive = datetime(2026, 7, 6, 4, 0, 0)  # naive datetimes are treated as UTC
    assert m.to_ms(naive) == m.to_ms(naive.replace(tzinfo=UTC))


def test_round_half_up_helper():
    assert m.round_half_up(2.675, 2) == 2.68   # Python's round() gives 2.67
    assert m.round_half_up(0.12345, 4) == 0.1235
    assert m.round_half_up(10.869565, 2) == 10.87


def test_count_weekdays():
    assert m.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15  # mid-month joiner
    assert m.count_weekdays(date(2026, 7, 11), date(2026, 7, 12)) == 0   # a weekend


def test_month_bounds_december_and_february():
    assert m.month_bounds("2026-12")[:2] == ("2026-12-01", "2026-12-31")
    assert m.month_bounds("2028-02")[:2] == ("2028-02-01", "2028-02-29")
