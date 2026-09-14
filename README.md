# GenEd Take-Home — Starter

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

## Docker

### Build

```bash
docker build -f .dockerfile -t gened-mastery .
```

### Run

The container stores the SQLite database at `/app/data/mastery.db` (set via
`MASTERY_DB_PATH`). Mount a named volume so the data survives container
restarts:

```bash
docker run --rm -p 8000:8000 -v gened-data:/app/data gened-mastery
```

Then visit http://localhost:8000/docs for the interactive API explorer.

To override the database path (e.g. for a custom mount):

```bash
docker run --rm -p 8000:8000 \
  -e MASTERY_DB_PATH=/app/data/custom.db \
  -v gened-data:/app/data \
  gened-mastery
```

> **Note:** Data is persisted in the `gened-data` Docker volume. To start
> fresh, remove it with `docker volume rm gened-data`.


