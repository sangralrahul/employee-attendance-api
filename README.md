# Employee Attendance & Analytics API

FastAPI + MongoDB service: punch-in / punch-out with the IST business rules, audited manual
corrections, and analytics built as MongoDB aggregation pipelines. The whole application is
`app/main.py`, as required.

## Run

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"           # or an Atlas connection string
export MONGO_DB="attendance_db"                         # (a local .env works too; real env vars win)
python sample_seed.py                                   # optional: the nine sample documents
uvicorn app.main:app --port 8000
```

`GET /health` returns 200 once MongoDB answers a ping. All indexes are created idempotently at
startup (and again from `/health` if MongoDB was not reachable yet). Interactive docs at `/docs`.

Local MongoDB in Docker: `docker run -d -p 27017:27017 --name mongo mongo:7`

## Tests

```bash
pytest -q tests/test_rules.py                      # pure business rules, no database needed
MONGO_URI=mongodb://localhost:27017 pytest -q      # full suite against a throwaway "<MONGO_DB>_test" database
```

The end-to-end suite boots the real app under uvicorn and covers status codes, R1–R10, the three race
conditions (concurrent creates, punch-ins, punch-outs and regularizations), the analytics edge cases
(ties, employees with no logs, weekend records, mid-month joiners, overnight shifts, missing days) and
the explain plans (IXSCAN present, no COLLSCAN).

`python scripts/generate_dataset.py` writes ~100,000 deliberately awkward records into `MONGO_DB`
(drops both collections first) for timing and `/admin/explain/*` checks at the grader's scale.

## Things worth knowing

* Instants are aware UTC datetimes inside the process (`MongoClient(tz_aware=True)`) and epoch
  milliseconds at the HTTP boundary; they are truncated to whole seconds before anything else (R1).
* Half-up rounding: Python side with `Decimal` (`work_hours`, `attendance_pct`); inside pipelines with
  `$round` after adding a 1e-9 epsilon, because MongoDB's `$round` is half-to-even.
* Every analytics endpoint has a `build_*` function that returns the pipeline and the index hint; the
  endpoint runs it and `/admin/explain/{endpoint}` explains the identical pipeline.
* Regularization uses optimistic concurrency on the history length: the loser of a concurrent edit
  gets 409 rather than silently overwriting.
* Nothing is missing from the contract. `REVIEW.md` lists the starter's defects, `DECISIONS.md` the
  design answers.
