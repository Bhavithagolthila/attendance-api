# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, attendance corrections (with an audit trail) and analytics.
All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then put your own MONGO_URI / MONGO_DB in .env
uvicorn app.main:app --port 8000
```

* `MONGO_URI` and `MONGO_DB` are read from environment variables; a local `.env` is only a convenience and
  real environment variables take priority. Nothing is hard-coded. Works with MongoDB Atlas or a local MongoDB 6.0+.
* All indexes are created automatically at startup (idempotent). No manual steps.
* Interactive docs: http://localhost:8000/docs

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py                 # business rules R1-R5, no database needed
pytest                                     # everything, against the MongoDB in MONGO_URI
TEST_USE_MOCK=1 pytest                     # no MongoDB: CRUD tests only, analytics/explain tests are skipped
```

The tests use their own database (`attendance_test_run`) and delete it afterwards; they never touch `MONGO_DB`.

## Notes

* Analytics endpoints are MongoDB aggregation pipelines (`$lookup`, `$setWindowFields` with `$rank` and a moving
  average, `$densify` for gap filling). They read the stored derived values and never recompute them.
* `GET /admin/explain/{endpoint}` runs `explain` (executionStats) on the same query/pipeline the real endpoint builds.
* `REVIEW.md` lists the defects found in the starter; `DECISIONS.md` answers the five design questions.
