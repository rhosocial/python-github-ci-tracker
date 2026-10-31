# Testing

## A real PostgreSQL is required

The suite binds the models to a real server and gives every test a freshly rebuilt
schema. SQLite was removed on 2026-11-01. The server comes from the environment or
from a shared scenarios file; CI uses a service container.

```bash
CITRACKER_DB_HOST=localhost CITRACKER_DB_PORT=5432 \
CITRACKER_DB_USER=citracker CITRACKER_DB_PASSWORD=citracker \
PYTHONPATH=tests .venv3.15/Scripts/python -m pytest tests -q
```

Without a server the tests **skip** and say why; in CI `CITRACKER_TEST_PG_REQUIRED=1`
turns that into a failure.

## Scope

Run what you changed. A full suite against a remote server takes 3–5 minutes.

```bash
PYTHONPATH=tests .venv3.15/Scripts/python -m pytest tests/core -q
PYTHONPATH=tests .venv3.15/Scripts/python -m pytest tests/controllers -q
```

## Static checks

```bash
.venv3.15/Scripts/ruff check src tests
.venv3.15/Scripts/black --check src tests
.venv3.15/Scripts/isort --check-only src tests
```
