# GenEd Take-Home — Starter

The actual assignment is in `PROBLEM.md` (sent alongside this repo) — read
that first. This README is just setup instructions.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

## Run

```bash
uvicorn mastery_service.main:app --reload
```

Then visit http://127.0.0.1:8000/docs for the interactive API explorer.

## Test

```bash
pytest
```

## What's given vs. what you build

- `mastery_service/ai_feedback.py` — given, do not rewrite. Simulates a
  slow/flaky AI provider on purpose.
- `mastery_service/seed_data.py` — given. Fake auth tokens, student/teacher
  roster, skill IDs.
- `mastery_service/main.py` — starter skeleton with the routes stubbed as
  TODOs. Everything else (data model, storage, mastery logic, rate
  limiting, notification durability) is yours to design and build. Feel
  free to restructure into more files.
- Storage: SQLite (or even in-memory, if you clearly document that
  trade-off) is fine — no need to stand up Postgres/Redis for this.

Submit your solution plus a `WRITEUP.md` per the instructions in
`PROBLEM.md`.
