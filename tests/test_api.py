"""API tests.

* CRUD / punch / regularize tests run with TEST_USE_MOCK=1 (no MongoDB) or against a real MongoDB.
* Tests marked real_mongo need a real MongoDB because they use $lookup pipelines,
  $setWindowFields, $densify and explain, which the in-memory mock cannot run.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

IST_OFFSET_MIN = 330


def ms(y, mo, d, h=0, mi=0, s=0):
    """IST wall-clock time -> epoch milliseconds."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) - IST_OFFSET_MIN * 60 * 1000


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def emp_body(code="EMP0001", **kw):
    body = {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com",
            "department": "Engineering", "joined_on": "2026-01-05"}
    body.update(kw)
    return body


# =========================================================================== #
# Employees
# =========================================================================== #
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_employee_and_duplicate(client, db):
    r = client.post("/employees", json=emp_body())
    assert r.status_code == 201
    body = r.json()
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30"
    assert isinstance(body["created_at"], int) and body["created_at"] > 10**12
    assert "_id" not in body
    assert client.post("/employees", json=emp_body()).status_code == 409


def test_create_employee_ignores_client_created_at(client, db):
    r = client.post("/employees", json=emp_body(created_at=5))
    assert r.status_code == 201 and r.json()["created_at"] != 5


@pytest.mark.parametrize("patch", [
    {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"emp_code": "emp0001"},
    {"email": "not-an-email"}, {"name": ""}, {"department": ""},
    {"shift_start": "9:30"}, {"shift_start": "25:00"},
    {"shift_start": "09:30", "shift_end": "09:30"},
    {"joined_on": "2026-02-30"}, {"joined_on": "05/01/2026"},
])
def test_create_employee_validation(client, db, patch):
    assert client.post("/employees", json=emp_body(**patch)).status_code == 422


def test_create_employee_missing_field(client, db):
    body = emp_body()
    del body["joined_on"]
    assert client.post("/employees", json=body).status_code == 422


def test_concurrent_employee_create_one_winner(client, db):
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: client.post("/employees", json=emp_body()).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7


def test_list_employees_pagination_filter_sort(client, db):
    for code, dept in [("EMP0003", "Sales"), ("EMP0001", "Engineering"), ("EMP0002", "Sales"), ("EMP0004", "Sales")]:
        client.post("/employees", json=emp_body(code, department=dept))
    r = client.get("/employees", params={"page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 4
    r = client.get("/employees", params={"page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"]
    r = client.get("/employees", params={"department": "Sales"}).json()
    assert r["total"] == 3 and [e["emp_code"] for e in r["items"]] == ["EMP0002", "EMP0003", "EMP0004"]
    assert client.get("/employees", params={"department": "sales"}).json()["total"] == 0  # case-sensitive


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_list_employees_bad_paging(client, db, params):
    assert client.get("/employees", params=params).status_code == 422


# =========================================================================== #
# Punch in / out
# =========================================================================== #
def test_punch_in_unknown_employee_is_404(client, db):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


def test_punch_in_basic_and_late(client, db):
    client.post("/employees", json=emp_body())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 15, 59)})
    assert r.status_code == 201
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 45 and b["status"] == "PRESENT"
    assert b["punch_out"] is None and b["work_hours"] is None
    assert b["overtime_minutes"] == 0 and b["half_day"] is False and b["history"] == []
    assert "id" not in b and "_id" not in b


def test_punch_in_truncates_to_whole_seconds(client, db):
    client.post("/employees", json=emp_body())
    p = ms(2026, 7, 6, 9, 40, 0) + 900  # 09:40:00.900 counts as 09:40:00 -> not late
    b = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": p}).json()
    assert b["late_minutes"] == 0 and b["punch_in"] == ms(2026, 7, 6, 9, 40, 0)


def test_punch_in_uses_ist_date(client, db):
    client.post("/employees", json=emp_body())
    # 00:30 IST on the 7th is 19:00 UTC on the 6th. The record's date is the IST date.
    b = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 30)}).json()
    assert b["date"] == "2026-07-07"


@pytest.mark.parametrize("punched_at", [1783312500, 1783312500000.5, "1783312500000", True, 99999999999, 4102444800001, -5])
def test_punch_in_bad_timestamp(client, db, punched_at):
    client.post("/employees", json=emp_body())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_at})
    assert r.status_code == 422


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "SICK"])
def test_punch_in_bad_status(client, db, status):
    client.post("/employees", json=emp_body())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status, "punched_at": ms(2026, 7, 6, 9, 30)})
    assert r.status_code == 422


@pytest.mark.parametrize("status", ["WFH", "ON_DUTY"])
def test_punch_in_other_presence_statuses(client, db, status):
    client.post("/employees", json=emp_body())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status, "punched_at": ms(2026, 7, 6, 9, 30)})
    assert r.status_code == 201 and r.json()["status"] == status


def test_punch_in_twice_same_day_409(client, db):
    client.post("/employees", json=emp_body())
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    assert client.post("/attendance/punch-in", json=body).status_code == 201
    assert client.post("/attendance/punch-in", json={**body, "punched_at": ms(2026, 7, 6, 11, 0)}).status_code == 409


def test_concurrent_punch_in_exactly_one_201(client, db):
    client.post("/employees", json=emp_body())
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: client.post("/attendance/punch-in", json=body).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_punch_out_full_flow(client, db):
    client.post("/employees", json=emp_body())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200
    b = r.json()
    assert b["work_hours"] == 9.08 and b["overtime_minutes"] == 40 and b["late_minutes"] == 35
    assert b["half_day"] is False and b["punch_out"] == ms(2026, 7, 6, 19, 10)
    assert b["history"] == []  # punching never writes history
    again = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 20)})
    assert again.status_code == 409


def test_punch_out_half_day_and_overtime_threshold(client, db):
    client.post("/employees", json=emp_body())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH", "punched_at": ms(2026, 7, 7, 9, 30)})
    b = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 13, 0)}).json()
    assert b["work_hours"] == 3.5 and b["half_day"] is True and b["overtime_minutes"] == 0
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 8, 9, 30)})
    b = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 8, 18, 59)}).json()
    assert b["overtime_minutes"] == 0  # 29 minutes is under the 30-minute minimum


def test_punch_out_errors(client, db):
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    client.post("/employees", json=emp_body())
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    # before the punch-in -> there is no record at or before that moment -> 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 404
    # same instant -> 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}).status_code == 422
    # more than 24 hours later -> 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30, 1)}).status_code == 422
    # exactly 24 hours later is allowed
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)}).status_code == 200


def test_concurrent_punch_out_exactly_one_200(client, db):
    client.post("/employees", json=emp_body())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: client.post("/attendance/punch-out", json=body).status_code, range(10)))
    assert sorted(codes) == [200] + [409] * 9


def test_overnight_shift_across_midnight(client, db):
    client.post("/employees", json=emp_body("EMP0005", department="Support", shift_start="22:00", shift_end="06:00"))
    b = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)}).json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 0
    b = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)}).json()
    assert b["work_hours"] == 8.75 and b["overtime_minutes"] == 40 and b["date"] == "2026-07-06"


def test_overnight_early_morning_punch_in_belongs_to_previous_day(client, db):
    client.post("/employees", json=emp_body("EMP0005", department="Support", shift_start="22:00", shift_end="06:00"))
    b = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 5, 30)}).json()
    assert b["date"] == "2026-07-06"


# =========================================================================== #
# GET /attendance
# =========================================================================== #
def seed_logs(db, rows):
    for emp, day, status in rows:
        doc = {"emp_code": emp, "date": day, "status": status, "punch_in": None, "punch_out": None,
               "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
        db.attendance_logs.insert_one(doc)


def test_list_attendance_sort_filter_paging(client, db):
    seed_logs(db, [("EMP0002", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-06", "PRESENT"),
                   ("EMP0001", "2026-07-07", "LEAVE"), ("EMP0003", "2026-07-08", "ABSENT"),
                   ("EMP0001", "2026-07-08", "WFH")])
    r = client.get("/attendance").json()
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0003"), ("2026-07-07", "EMP0001"),
        ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    assert r["total"] == 5
    r = client.get("/attendance", params={"emp_code": "EMP0001"}).json()
    assert [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07", "2026-07-06"] and r["total"] == 3
    r = client.get("/attendance", params={"status": "LEAVE"}).json()
    assert r["total"] == 1 and r["items"][0]["status"] == "LEAVE"
    r = client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1  # both bounds inclusive
    r = client.get("/attendance", params={"page": 2, "page_size": 2}).json()
    assert r["total"] == 5 and len(r["items"]) == 2 and r["page"] == 2


def test_list_attendance_defaults_for_legacy_documents(client, db):
    db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                   "punch_in": utc(2026, 7, 14, 4, 2), "punch_out": utc(2026, 7, 14, 13, 1),
                                   "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    item = client.get("/attendance").json()["items"][0]
    assert item["half_day"] is False and item["history"] == []
    assert item["punch_in"] == int(utc(2026, 7, 14, 4, 2).timestamp() * 1000)
    assert "id" not in item and "_id" not in item


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-08", "date_to": "2026-07-07"}, {"date_from": "07-08-2026"},
    {"date_from": "2026-02-30"}, {"status": "NOPE"}, {"page": 0}, {"page_size": 101},
])
def test_list_attendance_validation(client, db, params):
    assert client.get("/attendance", params=params).status_code == 422


# =========================================================================== #
# PATCH /attendance/{emp_code}/{date}
# =========================================================================== #
def make_record(client):
    client.post("/employees", json=emp_body())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 19, 0)})


def patch(client, **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return client.patch("/attendance/EMP0001/2026-07-07", json=body)


def test_regularize_recomputes_and_appends_history(client, db):
    make_record(client)
    r = patch(client, punch_in=ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.53 and b["overtime_minutes"] == 30
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h["changes"]["work_hours"] == {"from": 8.92, "to": 9.53}
    assert "status" not in h["changes"] and "half_day" not in h["changes"]  # only what changed
    # a second correction appends, never overwrites
    b = patch(client, status="WFH", reason="worked from home").json()
    assert len(b["history"]) == 2 and b["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    assert b["history"][0] == h


def test_regularize_to_absent_clears_punches(client, db):
    make_record(client)
    b = patch(client, status="ABSENT").json()
    assert b["punch_in"] is None and b["punch_out"] is None and b["work_hours"] is None
    assert b["late_minutes"] == 0 and b["overtime_minutes"] == 0 and b["half_day"] is False
    assert set(b["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"}
    # ...and back to a presence status needs a punch_in
    assert patch(client, status="PRESENT").status_code == 422
    b = patch(client, status="PRESENT", punch_in=ms(2026, 7, 7, 9, 30)).json()
    assert b["status"] == "PRESENT" and b["punch_out"] is None and b["work_hours"] is None


def test_regularize_rules(client, db):
    make_record(client)
    assert patch(client, status="LEAVE", punch_in=ms(2026, 7, 7, 9, 30)).status_code == 422
    assert patch(client).status_code == 422  # changes nothing
    assert patch(client, status="PRESENT").status_code == 422  # same status, changes nothing
    assert patch(client, punch_in=ms(2026, 7, 8, 9, 30)).status_code == 422  # off the attendance date
    assert patch(client, punch_out=ms(2026, 7, 7, 9, 0)).status_code == 422  # before punch_in
    assert patch(client, punch_out=ms(2026, 7, 8, 11, 0)).status_code == 422  # over 24 h
    assert patch(client, reason="abc").status_code == 422  # reason too short
    assert patch(client, work_hours=3).status_code == 422  # nothing to change: derived field ignored
    assert client.patch("/attendance/EMP0001/2026-07-07", json={"punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert db.attendance_logs.find_one({"emp_code": "EMP0001"})["history"] == []  # failures wrote nothing


def test_regularize_404_and_bad_date(client, db):
    make_record(client)
    assert client.patch("/attendance/EMP9999/2026-07-07", json={"status": "LEAVE", "reason": "valid reason", "regularized_by": "x"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-07-09", json={"status": "LEAVE", "reason": "valid reason", "regularized_by": "x"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-13-45", json={"status": "LEAVE", "reason": "valid reason", "regularized_by": "x"}).status_code == 422
    assert client.patch("/attendance/EMP0001/yesterday", json={"status": "LEAVE", "reason": "valid reason", "regularized_by": "x"}).status_code == 422


def test_regularize_ignores_client_sent_derived_fields(client, db):
    make_record(client)
    b = patch(client, status="WFH", work_hours=1.0, late_minutes=99, half_day=True).json()
    assert b["work_hours"] == 8.92 and b["late_minutes"] == 35 and b["half_day"] is False


def test_concurrent_regularize_never_loses_history(client, db):
    make_record(client)

    def one(i):
        return patch(client, status="WFH" if i % 2 else "ON_DUTY", reason=f"concurrent edit {i}").status_code

    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(one, range(8)))
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    ok = codes.count(200)
    assert ok >= 1 and all(c in (200, 409, 422) for c in codes)
    assert len(stored["history"]) == ok  # every 200 has exactly one history entry; nothing lost


# =========================================================================== #
# Analytics (need a real MongoDB)
# =========================================================================== #
EMPLOYEES = [
    ("EMP0001", "Engineering", "09:30", "18:30", "2026-01-05"),
    ("EMP0002", "Engineering", "09:30", "18:30", "2026-07-13"),  # mid-month joiner
    ("EMP0003", "Sales", "09:30", "18:30", "2026-02-01"),
    ("EMP0004", "Sales", "09:30", "18:30", "2026-02-01"),  # no logs at all
    ("EMP0005", "Support", "22:00", "06:00", "2026-03-10"),  # overnight shift
    ("EMP0006", "Support", "09:30", "18:30", "2026-08-03"),  # joins after July
    ("EMP0007", "Sales", "09:30", "18:30", "2026-02-01"),
]

# (emp, date, status, work_hours, late, overtime, half_day).   2026-07-06 is a Monday.
LOGS = [
    ("EMP0001", "2026-07-06", "PRESENT", 9.0, 0, 0, False),
    ("EMP0001", "2026-07-07", "PRESENT", 9.08, 35, 40, False),
    ("EMP0001", "2026-07-08", "WFH", 3.5, 0, 0, True),
    ("EMP0001", "2026-07-09", "LEAVE", None, 0, 0, False),
    ("EMP0001", "2026-07-11", "PRESENT", 8.0, 20, 60, False),  # Saturday; 07-10 has no record
    ("EMP0002", "2026-07-14", "PRESENT", 8.98, 0, 0, False),
    ("EMP0002", "2026-07-15", "ON_DUTY", 7.0, 0, 0, False),
    ("EMP0002", "2026-07-16", "PRESENT", 8.0, 10, 0, False),
    ("EMP0003", "2026-07-06", "PRESENT", 9.0, 35, 40, False),
    ("EMP0003", "2026-07-07", "ABSENT", None, 0, 0, False),
    ("EMP0003", "2026-07-08", "LEAVE", None, 0, 0, False),
    ("EMP0007", "2026-07-06", "PRESENT", 9.0, 35, 0, False),
    ("EMP0007", "2026-07-07", "PRESENT", 8.0, 12, 0, False),
    ("EMP0007", "2026-07-11", "PRESENT", 5.0, 0, 0, False),  # Saturday
    ("EMP0005", "2026-07-06", "PRESENT", 8.65, 0, 40, False),
    ("EMP0005", "2026-07-08", "PRESENT", 9.0, 35, 0, False),
    ("EMP9999", "2026-07-06", "PRESENT", 9.0, 100, 0, False),  # employee does not exist -> ignored
]


@pytest.fixture()
def data(client, db):
    for code, dept, s, e, joined in EMPLOYEES:
        db.employees.insert_one({"emp_code": code, "name": f"Name {code[-1]}", "email": f"{code}@x.com", "department": dept,
                                 "shift_start": s, "shift_end": e, "joined_on": joined, "created_at": utc(2026, 1, 1)})
    for emp, day, status, wh, late, ot, half in LOGS:
        doc = {"emp_code": emp, "date": day, "status": status, "punch_in": None, "punch_out": None,
               "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "history": []}
        if half:
            doc["half_day"] = True  # all other documents deliberately omit half_day (legacy shape)
        db.attendance_logs.insert_one(doc)
    return db


@pytest.mark.real_mongo
def test_employee_monthly(client, data):
    r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    assert r.json() == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 2.5,
                        "leave_days": 1, "late_count": 2, "total_late_minutes": 55, "total_overtime_minutes": 100,
                        "attendance_pct": 10.87}


@pytest.mark.real_mongo
def test_employee_monthly_mid_month_joiner_and_edge_cases(client, data):
    b = client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert b["working_days"] == 15 and b["present_days"] == 3 and b["attendance_pct"] == 20.0
    b = client.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()  # no logs at all
    assert b["working_days"] == 23 and b["present_days"] == 0 and b["attendance_pct"] == 0.0
    b = client.get("/analytics/employees/EMP0006/monthly", params={"month": "2026-07"}).json()  # not joined yet
    assert b["working_days"] == 0 and b["attendance_pct"] is None
    b = client.get("/analytics/employees/EMP0005/monthly", params={"month": "2026-07"}).json()
    assert b["attendance_pct"] == 8.7  # 2 / 23 * 100 = 8.6957


@pytest.mark.real_mongo
def test_employee_monthly_errors(client, data):
    assert client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "202607"}).status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly").status_code == 422


@pytest.mark.real_mongo
def test_department_summary(client, data):
    r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert r.status_code == 200
    items = {i["department"]: i for i in r.json()["items"]}
    assert list(items) == ["Engineering", "Sales", "Support"]  # sorted by department
    assert items["Engineering"] == {"department": "Engineering", "headcount": 2, "present_days": 5.5, "avg_work_hours": 7.65,
                                    "late_count": 3, "total_late_minutes": 65, "leave_count": 1, "on_duty_count": 1}
    # Sales headcount 3 includes EMP0004, who has no logs at all; 31 h / 4 records = 7.75
    assert items["Sales"] == {"department": "Sales", "headcount": 3, "present_days": 3.0, "avg_work_hours": 7.75,
                              "late_count": 3, "total_late_minutes": 82, "leave_count": 1, "on_duty_count": 0}
    # EMP0006 joins in August and is not counted; average (8.65 + 9.0) / 2 = 8.825 -> 8.83 (half-up)
    assert items["Support"] == {"department": "Support", "headcount": 1, "present_days": 2.0, "avg_work_hours": 8.83,
                                "late_count": 1, "total_late_minutes": 35, "leave_count": 0, "on_duty_count": 0}


@pytest.mark.real_mongo
def test_department_summary_filters_and_empty_cases(client, data):
    r = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in r["items"]] == ["Sales"]
    assert client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Nope"}).json()["items"] == []
    # In January only EMP0001 (Engineering) had joined: other departments are omitted (headcount 0)
    r = client.get("/analytics/departments/summary", params={"month": "2026-01"}).json()
    assert [(i["department"], i["headcount"], i["avg_work_hours"]) for i in r["items"]] == [("Engineering", 1, None)]
    assert client.get("/analytics/departments/summary").status_code == 422


@pytest.mark.real_mongo
def test_late_leaderboard_ties_and_limit_after_ranking(client, data):
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()
    rows = [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in r["items"]]
    # EMP0003 and EMP0005 tie at 35 -> both rank 3, EMP0002 (10) is rank 5. EMP9999 is ignored.
    assert rows == [(1, "EMP0001", 55), (2, "EMP0007", 47), (3, "EMP0003", 35), (3, "EMP0005", 35)]
    assert r["items"][0] == {"rank": 1, "emp_code": "EMP0001", "name": "Name 1", "department": "Engineering",
                             "total_late_minutes": 55, "late_count": 2}
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()
    assert [i["emp_code"] for i in r["items"]] == ["EMP0001", "EMP0007"]
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50}).json()
    assert [(i["rank"], i["emp_code"]) for i in r["items"]] == [(1, "EMP0001"), (2, "EMP0007"), (3, "EMP0003"), (3, "EMP0005"), (5, "EMP0002")]


@pytest.mark.real_mongo
def test_late_leaderboard_department_and_validation(client, data):
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Sales"}).json()
    assert [(i["rank"], i["emp_code"]) for i in r["items"]] == [(1, "EMP0007"), (2, "EMP0003")]  # ranked inside Sales
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-08"}).json()["items"] == []
    for bad in ({"limit": 0}, {"limit": 51}, {"limit": "x"}):
        assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", **bad}).status_code == 422


@pytest.mark.real_mongo
def test_department_trend_gap_filling_and_moving_average(client, data):
    r = client.get("/analytics/departments/Sales/trend", params={"from": "2026-07-05", "to": "2026-07-12"})
    assert r.status_code == 200
    rows = r.json()["items"]
    assert [x["date"] for x in rows] == [f"2026-07-{d:02d}" for d in range(5, 13)]  # exactly one row per day
    got = [(x["is_working_day"], x["headcount"], x["present_count"], x["late_count"], x["attendance_rate"], x["moving_avg_7d"])
           for x in rows]
    assert got == [
        (False, 3, 0, 0, None, None),            # Sun
        (True, 3, 2, 2, 0.6667, 0.6667),         # Mon
        (True, 3, 1, 1, 0.3333, 0.5),            # Tue
        (True, 3, 0, 0, 0.0, 0.3333),            # Wed (only a LEAVE record)
        (True, 3, 0, 0, 0.0, 0.25),              # Thu (no records at all -> rate 0.0)
        (True, 3, 0, 0, 0.0, 0.2),               # Fri
        (False, 3, 1, 0, None, 0.2),             # Sat (a weekend record counts as present, but no rate)
        (False, 3, 0, 0, None, 0.2),             # Sun
    ]


@pytest.mark.real_mongo
def test_department_trend_no_logs_in_range_and_joiners(client, data):
    rows = client.get("/analytics/departments/Support/trend", params={"from": "2026-07-01", "to": "2026-07-03"}).json()["items"]
    assert [(x["date"], x["attendance_rate"], x["moving_avg_7d"]) for x in rows] == [
        ("2026-07-01", 0.0, 0.0), ("2026-07-02", 0.0, 0.0), ("2026-07-03", 0.0, 0.0)]
    # Engineering: EMP0002 joins on 07-13, so the headcount goes from 1 to 2 on that day
    rows = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-12", "to": "2026-07-14"}).json()["items"]
    assert [x["headcount"] for x in rows] == [1, 2, 2]
    assert rows[2]["present_count"] == 1 and rows[2]["attendance_rate"] == 0.5
    # a single-day range works
    assert len(client.get("/analytics/departments/Sales/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).json()["items"]) == 1


@pytest.mark.real_mongo
def test_department_trend_errors(client, data):
    base = {"from": "2026-07-01", "to": "2026-07-10"}
    assert client.get("/analytics/departments/Nope/trend", params=base).status_code == 404
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    # 2026-01-01 .. 2026-04-02 is exactly 92 days (allowed); one more day is rejected
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422


# =========================================================================== #
# Explain (needs a real MongoDB)
# =========================================================================== #
def plan_text(resp):
    import json
    return json.dumps(resp.json()["explain"])


@pytest.mark.real_mongo
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "LEAVE", "page": 2, "page_size": 5}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 5}),
    ("late_leaderboard", {"month": "2026-07", "department": "Sales"}),
    ("department_trend", {"department": "Sales", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_index_and_never_collscan(client, data, endpoint, params):
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    text = plan_text(r)
    assert "IXSCAN" in text
    assert "COLLSCAN" not in text


@pytest.mark.real_mongo
@pytest.mark.parametrize("endpoint,params", [
    ("employee_monthly", {"month": "2026-07"}), ("employee_monthly", {"emp_code": "EMP0001"}),
    ("department_summary", {}), ("late_leaderboard", {}),
    ("department_trend", {"department": "Sales"}), ("department_trend", {"from": "2026-07-01", "to": "2026-07-05"}),
    ("not_an_endpoint", {}),
])
def test_explain_missing_params_422(client, data, endpoint, params):
    assert client.get(f"/admin/explain/{endpoint}", params=params).status_code == 422
