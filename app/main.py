"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env file is loaded for convenience;
                             real environment variables always win)

Everything lives in this one file, in this order:
  1. Setup (database connection, constants, indexes)
  2. Small helpers (time zone, rounding, the business rules R1-R5)
  3. Request models (validation -> 422)
  4. Employee and attendance endpoints
  5. Analytics endpoints (MongoDB aggregation pipelines)
  6. Admin explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

# override=False (the default): a real environment variable beats the .env file.
load_dotenv()
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
# tz_aware=True makes PyMongo return datetimes WITH a UTC time zone.
# (By default it returns "naive" datetimes, which cannot be mixed with IST-aware ones.)
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5

MIN_MS = 100_000_000_000  # below this the number looks like seconds -> 422
MAX_MS = 4_102_444_800_000  # year 2100

DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# Index names are fixed so the endpoints can ask MongoDB to use them (hint).
IDX_LOG_EMP_DATE = "emp_code_1_date_1"  # unique: one record per employee per day
IDX_LOG_DATE_EMP = "date_-1_emp_code_1"  # list sorted by date desc, emp_code asc
IDX_LOG_STATUS_DATE = "status_1_date_-1_emp_code_1"  # list filtered by status
IDX_LOG_EMP_PUNCHIN = "emp_code_1_punch_in_-1"  # punch-out: latest punch-in <= time
IDX_EMP_CODE = "emp_code_1"  # unique employee code
IDX_EMP_DEPT = "department_1_emp_code_1"  # filter by department, sorted by code
IDX_EMP_JOINED = "joined_on_1"  # headcount: joined_on <= end of month


def ensure_indexes():
    """Create every index. create_index is idempotent: running it again is harmless."""
    specs = [
        (db.attendance_logs, [("emp_code", 1), ("date", 1)], IDX_LOG_EMP_DATE, True),
        (db.attendance_logs, [("date", -1), ("emp_code", 1)], IDX_LOG_DATE_EMP, False),
        (db.attendance_logs, [("status", 1), ("date", -1), ("emp_code", 1)], IDX_LOG_STATUS_DATE, False),
        (db.attendance_logs, [("emp_code", 1), ("punch_in", -1)], IDX_LOG_EMP_PUNCHIN, False),
        (db.employees, [("emp_code", 1)], IDX_EMP_CODE, True),
        (db.employees, [("department", 1), ("emp_code", 1)], IDX_EMP_DEPT, False),
        (db.employees, [("joined_on", 1)], IDX_EMP_JOINED, False),
    ]
    for coll, keys, name, unique in specs:
        try:
            coll.create_index(keys, name=name, unique=unique)
        except ServerSelectionTimeoutError:
            log.error("MongoDB is not reachable; skipping index creation (/health will return 503)")
            return  # do not wait for a timeout once per index
        except Exception:  # keep the app alive; /health still tells the truth
            log.exception("could not create index %s", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()  # runs once, at startup
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
def unprocessable(message: str, loc: str = "body") -> HTTPException:
    """A 422 with the same shape as FastAPI's own validation errors."""
    return HTTPException(422, [{"loc": [loc], "msg": message, "type": "value_error"}])


def ensure_aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC (defensive; with tz_aware=True it should not happen)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> integer epoch milliseconds (exact integer maths, no floats)."""
    if dt is None:
        return None
    return (ensure_aware(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def parse_date(value: str, loc: str = "date") -> date_cls:
    """'YYYY-MM-DD' -> date. Rejects impossible dates such as 2026-02-30 with a 422."""
    if not re.match(DATE_RE, value):
        raise unprocessable("date must look like YYYY-MM-DD", loc)
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise unprocessable("not a real calendar date", loc)


def round_half_up(value, places: int) -> float:
    """Round half-up (what the contract asks for). Python's round() is banker's rounding."""
    quantum = Decimal(1).scaleb(-places)
    # round(.., 9) first removes binary float noise such as 0.1499999999999
    return float(Decimal(repr(round(float(value), 9))).quantize(quantum, rounding=ROUND_HALF_UP))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: the IST calendar date of the punch-in; an overnight shift punched in
    before shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def shift_bounds(day: date_cls, shift_start: str, shift_end: str):
    """Start and end of the shift that belongs to attendance date `day` (both IST-aware)."""
    start = datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(day, parse_hhmm(shift_end), tzinfo=IST)
    if end <= start:  # overnight: the shift ends on the next calendar day
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: date_cls, shift_start: str, shift_end: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start;
    then whole minutes since shift start (not since the grace period ended)."""
    start, _ = shift_bounds(day, shift_start, shift_end)
    seconds = (punch_in - start).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: date_cls, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if at least 30; otherwise 0."""
    _, end = shift_bounds(day, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5: the ROUNDED work hours are below 4.50."""
    return Decimal(str(work_hours)) < HALF_DAY_BELOW


def compute_derived(emp: dict, day: date_cls, status: str, punch_in, punch_out) -> dict:
    """All derived fields for one record, from its final status and punch times."""
    if status not in PRESENT_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "late_minutes": compute_late_minutes(punch_in, day, emp["shift_start"], emp["shift_end"]),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):  # nested BSON dates -> epoch ms
            changes[field] = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        else:
            changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": to_ms(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """MongoDB attendance document -> API response. No _id; missing legacy fields get defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def month_bounds(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31', date(2026,7,1), date(2026,7,31))."""
    year, mon = int(month[:4]), int(month[5:7])
    first = date_cls(year, mon, 1)
    nxt = date_cls(year + (mon == 12), mon % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat(), first, last


def count_weekdays(start: date_cls, end: date_cls) -> int:
    """Number of Mon-Fri days from start to end inclusive (no holiday calendar, R7)."""
    days = (end - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
# strict=True: 1.5, "123" and true are all rejected (422); the range rejects seconds.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_date(cls, v: str) -> str:
        if not re.match(DATE_RE, v):
            raise ValueError("joined_on must look like YYYY-MM-DD")
        date_cls.fromisoformat(v)  # raises ValueError for 2026-02-30
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # Derived fields (work_hours, ...) sent by a client are simply ignored by pydantic.


# --------------------------------------------------------------------------- #
# 4. Employee and attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # The UNIQUE index on emp_code decides who wins when two requests race.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)  # total of the FILTERED set
    cursor = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])  # R1
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **compute_derived(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        # No "check then insert": the unique (emp_code, date) index lets exactly one request in.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before the punch-out time.
    # (Records without a punch_in, i.e. ABSENT/LEAVE, never match a date comparison.)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = ensure_aware(rec["punch_in"])
    if ts <= p_in:
        raise unprocessable("punched_at must be after the punch-in", "punched_at")
    if ts - p_in > timedelta(hours=24):
        raise unprocessable("punch-out is more than 24 hours after the punch-in", "punched_at")

    derived = compute_derived(emp, date_cls.fromisoformat(rec["date"]), rec["status"], p_in, ts)
    derived.pop("late_minutes")  # late_minutes was set at punch-in; leave it alone
    # Atomic "close it only if it is still open": of two simultaneous punch-outs, one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out (or the record changed meanwhile)")
    return record_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status):
    """Returns (filter, sort, index name). Shared by GET /attendance and the explain endpoint."""
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    if emp_code is not None:
        # emp_code is fixed, so sorting by date alone is already a total order.
        return q, [("date", -1)], IDX_LOG_EMP_DATE
    if status is not None:
        return q, [("date", -1), ("emp_code", 1)], IDX_LOG_STATUS_DATE
    return q, [("date", -1), ("emp_code", 1)], IDX_LOG_DATE_EMP


def check_date_range(date_from: Optional[str], date_to: Optional[str]):
    d1 = parse_date(date_from, "date_from") if date_from is not None else None
    d2 = parse_date(date_to, "date_to") if date_to is not None else None
    if d1 and d2 and d1 > d2:
        raise unprocessable("date_from must not be after date_to", "date_from")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_date_range(date_from, date_to)
    q, sort, hint = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q, hint=hint)
    # Sorting and paging happen inside MongoDB (via the index), never in Python.
    cursor = db.attendance_logs.find(q).hint(hint).sort(sort).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, date: str = Path(pattern=DATE_RE)):
    parse_date(date, "date")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_in = ensure_aware(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = ensure_aware(rec["punch_out"]) if rec.get("punch_out") else None
    new_status = body.status or rec["status"]
    new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
    new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("ABSENT/LEAVE clear the punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        if new_in is None:
            raise unprocessable("a presence status needs a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise unprocessable("punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
            raise unprocessable("punch_out must be after punch_in and within 24 hours")

    day = date_cls.fromisoformat(date)
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **compute_derived(emp, day, new_status, new_in, new_out)}
    old = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {f: {"from": old[f], "to": new[f]} for f in new if new[f] != old[f]}
    if not changes:
        raise unprocessable("this request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # Optimistic lock: only write if the history still has exactly n entries and the
    # fields we read are unchanged. Otherwise somebody else edited it first -> 409.
    guard = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"),
             "punch_out": rec.get("punch_out"), f"history.{n}": {"$exists": False}}
    if n > 0:
        guard[f"history.{n - 1}"] = {"$exists": True}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by someone else; please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 5. Analytics (MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
# Small expression pieces reused by several pipelines.
def _is_present():
    return {"$in": ["$status", PRESENT_STATUSES]}


def _present_weight():
    """1 for a present day, 0.5 for a half day (a missing half_day field means false)."""
    return {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}


def _late():
    return {"$ifNull": ["$late_minutes", 0]}  # missing means 0


def _is_weekday():
    # $isoDayOfWeek: Monday=1 ... Sunday=7. The date string is parsed as a plain calendar date.
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}


def stats_group(group_id):
    """One $group stage that produces every per-employee counter the analytics need.
    It READS the stored derived values (late_minutes, ...) and never recomputes them."""
    has_hours = {"$and": [_is_present(), {"$isNumber": "$work_hours"}]}
    return {"$group": {
        "_id": group_id,
        "present_days": {"$sum": {"$cond": [{"$and": [_is_present(), _is_weekday()]}, _present_weight(), 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [_late(), 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$cond": [{"$gt": [_late(), 0]}, _late(), 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
        "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }}


def build_monthly_pipeline(emp_code: str, month: str):
    start, end, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        stats_group(None),
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    emp = get_employee_or_404(emp_code)
    _, _, first, last = month_bounds(month)
    joined = parse_date(emp["joined_on"], "joined_on")
    working_days = count_weekdays(max(first, joined), last)  # R7: counted from joined_on

    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, month), hint=IDX_LOG_EMP_DATE))
    s = rows[0] if rows else {}
    present_days = s.get("present_days", 0)
    pct = round_half_up(Decimal(str(present_days)) / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": s.get("leave_count", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


def build_summary_pipeline(month: str, department: Optional[str]):
    """Starts from EMPLOYEES (so people with zero logs still count) and joins each
    employee's month of logs with $lookup. Employees who have not joined yet are excluded."""
    start, end, _, _ = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, stats_group(None)],
            "as": "s",
        }},
        # An employee without logs has s == [] -> $first gives "missing" -> $ifNull makes it 0.
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": [{"$first": "$s.present_days"}, 0]}},
            "hours_sum": {"$sum": {"$ifNull": [{"$first": "$s.hours_sum"}, 0]}},
            "hours_n": {"$sum": {"$ifNull": [{"$first": "$s.hours_n"}, 0]}},
            "late_count": {"$sum": {"$ifNull": [{"$first": "$s.late_count"}, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": [{"$first": "$s.total_late_minutes"}, 0]}},
            "leave_count": {"$sum": {"$ifNull": [{"$first": "$s.leave_count"}, 0]}},
            "on_duty_count": {"$sum": {"$ifNull": [{"$first": "$s.on_duty_count"}, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def summary_hint(department: Optional[str]) -> str:
    return IDX_EMP_DEPT if department is not None else IDX_EMP_JOINED


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    rows = db.employees.aggregate(build_summary_pipeline(month, department), hint=summary_hint(department))
    items = []
    for r in rows:
        avg = round_half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    start, end, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees have no match and disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4 (ties share a rank, next rank skipped)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department), hint=IDX_LOG_DATE_EMP))
    return {"month": month, "items": items}


def check_trend_range(d_from: str, d_to: str):
    a, b = parse_date(d_from, "from"), parse_date(d_to, "to")
    if b < a:
        raise unprocessable("to must not be before from", "to")
    if (b - a).days + 1 > 92:
        raise unprocessable("the range must not exceed 92 days", "to")
    return a, b


def load_department_staff(department: str):
    """(emp_codes, joined_on dates) of one department; 404 if it has no employees."""
    staff = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}).hint(IDX_EMP_DEPT))
    if not staff:
        raise HTTPException(404, "unknown department")
    return [s["emp_code"] for s in staff], [s["joined_on"] for s in staff]


def build_trend_pipeline(department: str, codes: list, joined: list, a: date_cls, b: date_cls):
    first, last = a.isoformat(), b.isoformat()
    present = _present_weight()
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": first, "$lte": last}}},
        # One harmless "seed" row on the first day. It makes sure $densify always has an input
        # document, even when the department has no logs at all in the range.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "date": {"$literal": first}}}]}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [_is_present(), present, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late(), 0]}, 1, 0]}},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # $densify = gap filling: one document per calendar day (upper bound is exclusive).
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [
            datetime(a.year, a.month, a.day, tzinfo=UTC),
            datetime(b.year, b.month, b.day, tzinfo=UTC) + timedelta(days=1)]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {  # headcount = employees with joined_on <= this day (R9)
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j",
                                                 "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        {"$addFields": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # Moving average over the current row and up to 6 rows before it. $avg skips nulls.
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "avg7": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "rate": 1, "avg7": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=DATE_RE),
    to: str = Query(pattern=DATE_RE),
):
    a, b = check_trend_range(from_, to)
    codes, joined = load_department_staff(department)
    rows = db.attendance_logs.aggregate(build_trend_pipeline(department, codes, joined, a, b), hint=IDX_LOG_EMP_DATE)
    items = [{
        "date": r["date"],
        "is_working_day": r["is_working_day"],
        "headcount": r["headcount"],
        "present_count": round_half_up(r["present_count"], 2),
        "late_count": r["late_count"],
        "attendance_rate": None if r["rate"] is None else round_half_up(r["rate"], 4),
        "moving_avg_7d": None if r["avg7"] is None else round_half_up(r["avg7"], 4),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 6. Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(**params):
    """422 if a required query parameter is missing."""
    for name, value in params.items():
        if value is None:
            raise unprocessable(f"query parameter '{name}' is required", name)


def _check_month(month: str):
    if not re.match(MONTH_RE, month):
        raise unprocessable("month must look like YYYY-MM", "month")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs MongoDB's explain (executionStats) on the SAME query/pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        check_date_range(date_from, date_to)
        q, sort, hint = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q, "sort": dict(sort), "skip": (page - 1) * page_size,
                   "limit": page_size, "hint": hint}
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            _check_month(month)
            collection, pipeline, hint = "attendance_logs", build_monthly_pipeline(emp_code, month), IDX_LOG_EMP_DATE
        elif endpoint == "department_summary":
            _need(month=month)
            _check_month(month)
            collection, pipeline, hint = "employees", build_summary_pipeline(month, department), summary_hint(department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            _check_month(month)
            collection, pipeline, hint = "attendance_logs", build_leaderboard_pipeline(month, limit, department), IDX_LOG_DATE_EMP
        else:  # department_trend
            _need(department=department, **{"from": from_, "to": to})
            a, b = check_trend_range(from_, to)
            codes, joined = load_department_staff(department)
            collection, pipeline, hint = "attendance_logs", build_trend_pipeline(department, codes, joined, a, b), IDX_LOG_EMP_DATE
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # The raw answer holds BSON types (dates, ObjectIds, ...): convert them to plain JSON.
    return JSONResponse({"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))})
