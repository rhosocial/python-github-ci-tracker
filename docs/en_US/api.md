# The API

MCP is the primary surface; REST and CLI are the same flows in other shapes.

## Starting the MCP service

```bash
docker compose --env-file docker/stack.env up -d mcp
# listens on 0.0.0.0:8001; CITRACKER_MCP_PORT moves it; bind to loopback when local
```

## The tools

```
ci_status                              what this service can do, and its health
find_runs(repository, ...)             runs by commit / PR / branch / workflow / status
get_run(run_id)                        the run and its job roster
get_job(job_id, refresh_logs=false)    one job, every step, and each has_log
get_job_log(job_id, step=N, ...)       one step, the failed steps, or the merged document
search_job_log(job_id, query, step=N)  search with line numbers
wait_for_run(run_id, timeout)          bounded long poll
trigger_workflow(..., if_absent=false) dispatch
recent_runs(repositories, ...)         several repositories in one call
rerun_run / cancel_run                 re-run and cancel
```

`workflow=` takes a **path, a file name or the numeric id** — never a display
name, because the name differs per branch and the stored one follows the last run
written.

## Authentication

Bearer `CITRACKER_API_KEY`. An empty key means no auth and warns at startup; on a
public endpoint it must be set.

## Prompts that work

Show the model the tool, not the shell. Two examples:

```text
Is rhosocial/python-activerecord green on main? Use recent_runs.

Did ce9fac5 pass CI? Use find_runs with a commit_sha.
```

## Keys and pagination

`limit` (max 50) and `offset`; every list reports `truncated` and `next_offset`.

## Time

Every timestamp UTC with an explicit offset. `created_at` is GitHub's creation
time, `stored_at` is when the row was written locally — they answer different
questions.

## One attempt

Re-runs keep the run id and increment the attempt number; pass `attempt=N` to
`get_run` for a past execution. Each attempt's jobs are the ones stored when it
ran.

## Common questions

- **A run that this service never heard about**: `find_runs` reads GitHub and
  adopts it, so no run is invisible.
- **A step with no log**: GitHub serves the split for about an hour; after that
  the answer is `capture_closed` and the reader falls back to the job's merged
  document.
- **A very long job**: the default window is the last 200 lines and says how much
  remains.

## When a trigger is allowed

`trigger_workflow` requires Actions write access on the bound credential and a
workflow that declares `workflow_dispatch`. `if_absent=true` answers "this commit
already ran" without spending minutes.
