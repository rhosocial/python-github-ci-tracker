# Development

## Virtual environment

```bash
py -3.15 -m venv .venv3.15

# siblings first, each on its release line
.venv3.15/Scripts/pip install -e ../python-activerecord          -i https://mirrors.aliyun.com/pypi/simple/
.venv3.15/Scripts/pip install -e ../python-activerecord-postgres -i https://mirrors.aliyun.com/pypi/simple/
.venv3.15/Scripts/pip install -e ../python-mcpserver             -i https://mirrors.aliyun.com/pypi/simple/

# this project and its dev dependencies
.venv3.15/Scripts/pip install -e ".[dev]" -i https://mirrors.aliyun.com/pypi/simple/
```

## Running the tests

See [Testing](testing.md). A PostgreSQL server is required; there is no SQLite
path any more.

## The codebase in one paragraph

`src/rhosocial/github_ci_tracker/` holds `models/` (table mappings, state
machines, payload parsing — the leaves), `services/` (the GitHub API surface),
`controllers/` (the flows, and the only place ORM queries are written),
`views.py` (what the adapters shape), `config.py` / `database.py` /
`container.py` (wiring), and `adapters/` (one per protocol).
