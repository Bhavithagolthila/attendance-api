# REVIEW.md

Defects found in the starter `app/main.py`. "R" numbers are the business rules in `openapi.yaml`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` at module level | No `tz_aware=True`, so every datetime read from MongoDB is naive and cannot be mixed with the IST-aware values the rules need. | `compute_overtime` raises `TypeError: can't subtract offset-naive and offset-aware datetimes` on any punch-out of a stored record. | `tz_aware=True`, plus `ensure_aware()` as a safety net. |
| 2 | `compute_late_minutes` | Puts the shift start on the **UTC** clock (`punch_in.replace(hour=9, minute=30)`) instead of IST. | Punch-in 10:05 IST (stored 04:35Z) compares against 09:30Z, so the result is negative and becomes 0. The sample record for EMP0003 must be 35. | Shift start is built as an IST datetime for the attendance date (`shift_bounds`). |
| 3 | `compute_late_minutes` | Floors to whole minutes **before** checking the grace period (`int(sec/60) > 10`). The rule is "strictly more than 10:00". | 09:40:30 floors to 10, which is not `> 10`, so it returns 0. R2 says 10. 09:40:01 has the same problem. | Compare seconds (`> 600`), then floor. |
| 4 | `compute_late_minutes`, `punch_in` | Ignores overnight shifts (R1): the shift start for a 22:00 shift punched at 05:30 is on the previous day. | Shift 22:00-06:00, punch-in 07-07 05:30: the record must be dated 07-06. | `attendance_date()` and `shift_bounds()` handle `shift_end <= shift_start`. |
| 5 | `compute_work_hours` | Python `round()` (banker's rounding on floats) and no truncation to whole seconds. R4 wants half-up. | 4h 30m 18s is 4.505 h and must give 4.51; the starter gives 4.5. Also affects the half-day decision (R5). | Integer seconds with `Decimal(...).quantize(ROUND_HALF_UP)`. |
| 6 | `compute_overtime` | No 30-minute minimum (R3), the shift end is always on the same date (breaks overnight), and the aware/naive mix raises `TypeError`. | Punch-out 18:35 on a 18:30 shift returns 5 (sample says 0). Overnight 22:00-06:00 punched out 06:40 next day gives about 1480. | `compute_overtime` uses the real shift end (next day for overnight) and returns 0 below 30. |
| 7 | `create_employee` | Check-then-insert race, and no unique index. | Fire 10 parallel POSTs with the same `emp_code`: several get 201 and duplicates are stored. | Unique index on `emp_code`; catch `DuplicateKeyError` and return 409. |
| 8 | `EmployeeIn` | No validation: bad `emp_code`, bad email, empty name, `25:99` shift times, `shift_start == shift_end`, `joined_on: "2026-02-30"` are all accepted. | POST `{"emp_code": "x", "shift_start": "99:99"}` returns 201. | Field patterns/lengths and validators, so all of these return 422. |
| 9 | `create_employee` | `datetime.now()` is naive local server time (not UTC), and `created_at` is returned as a datetime/ISO text instead of epoch milliseconds. | Response `created_at` is a string. | `now_utc()` stored as a BSON date; `employee_out()` returns epoch ms. |
| 10 | `list_employees` | `skip = page * page_size` is off by one page. | `page=1` skips the first 20 employees and never shows them. | `skip = (page - 1) * page_size`. |
| 11 | `list_employees` | `total` is `count_documents({})`, ignoring the department filter (R10). | `?department=Sales` returns 3 items and `total: 6`. | Count with the same filter. |
| 12 | `list_employees` | No sort (unstable paging; contract says `emp_code` ascending) and no bounds on `page` / `page_size`. | `page_size=0`, `page=0` (negative skip, so a 500), `page_size=100000` all accepted. | `.sort("emp_code")`; `Query(ge=1)` and `le=100` give 422. |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` raises `TypeError` (500). | POST punch-in for `EMP9999`. | `get_employee_or_404`. |
| 14 | `punch_in` | Check-then-insert race: two simultaneous punch-ins both pass `find_one`. | 10 parallel identical requests give several 201s and duplicate records. | Unique `(emp_code, date)` index; `DuplicateKeyError` means 409. |
| 15 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` is server-local naive time, float maths, not truncated to seconds; `if body.punched_at` treats 0 as "now"; `Optional[int]` happily coerces `"123"` and `1.0`. | Results change with the server's time zone. `punched_at: 1783312500` (seconds) is accepted. Contract wants 422. | `EpochMs` strict integer within range; `from_ms()` builds a UTC datetime truncated to whole seconds. |
| 16 | `punch_in` | `date` is `ts.date()` (server-local), not the IST date, and the overnight rule is missing (R1). | Punch-in at 00:30 IST gets yesterday's date on a UTC server. | `attendance_date()`. |
| 17 | `PunchInIn.status`, `punch_in` | `status: str` accepts `ABSENT`, `LEAVE`, or any text. | POST `status: "LEAVE"` returns 201. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 18 | `punch_in`, `list_attendance` | The response leaks Mongo's `_id` as `id` (the contract says it never appears) and returns raw datetimes instead of epoch ms; legacy documents without `half_day` / `history` are not defaulted. | Any response has an `id` field. | `record_out()` builds the response explicitly with defaults. |
| 19 | `list_attendance` | Loads every matching document into Python, sorts and pages there. Very slow at 100k records and uses lots of memory. | Time `GET /attendance` on 100k documents. | `count_documents` + `find().hint().sort().skip().limit()` inside MongoDB, backed by indexes. |
| 20 | `list_attendance` | Sorted by `date` only (no `emp_code` tie-break), so pages are unstable. | Two records on the same date can swap between pages. | Sort `date` desc, `emp_code` asc (index-backed). |
| 21 | `list_attendance` | No validation: `date_from > date_to`, malformed dates, `page=0` (slice `[-20:0]`), unlimited `page_size`. | `?date_from=2026-07-10&date_to=2026-07-01` returns 200 and an empty list. | Pattern and real-date checks and the range check give 422; `Query` bounds on paging. |
| 22 | whole app | No indexes are created at startup, so every query scans and nothing enforces uniqueness. | `explain` shows `COLLSCAN`. | `ensure_indexes()` runs from the app's startup (lifespan), idempotent. |
| 23 | `/health` | Always returns `ok`, even when MongoDB is down (contract: 503). | Stop MongoDB and call `/health`. | `ping` the database; 503 if it fails. |

## Looked at and decided it was fine

* `load_dotenv()` with its default `override=False`: real environment variables already win over `.env`, as the contract requires.
* `IST = timezone(timedelta(hours=5, minutes=30))` is correct (India has no daylight saving time).
* Default shift times `09:30` / `18:30` match `openapi.yaml`.
* `status_code=201` on `POST /employees` and `POST /attendance/punch-in` matches the contract.
* Pydantic's default of ignoring unknown body fields is what the contract wants for client-sent derived fields (`work_hours`, ...).
* Plain (non-async) endpoint functions: FastAPI runs them in a thread pool, which is the right model for the blocking PyMongo driver.
* Storing `date` / `joined_on` as `YYYY-MM-DD` strings: lexicographic comparison equals date comparison, so range filters are correct.
