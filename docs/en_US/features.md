# Features

## Three object layers, one tool each

There is exactly one path from a failing run to the failing step:

```
get_run(run_id)              → the run and its job roster
get_job(job_id)              → one job with every step (status, conclusion, has_log)
get_job_log(job_id, step=N)  → that step's text
get_job_log(job_id)          → the job's merged document (the fallback)
```

- Steps belong to `get_job` only: `get_run` has no `include_steps` and no failure block.
- `failed_steps_only=true` is a batch convenience, not a second way to decide which
  step failed.
- A green job is not pushed to read logs — `has_log` is still honestly `true`.

## Two log sources

| Source | Content | Availability |
|---|---|---|
| `GET /actions/jobs/{id}/logs` | a job's merged text | any time within 90 days, stored in `ct_job_logs` |
| `GET /actions/runs/{id}/logs` | zip: merged file + `<job>/<step no>_<name>.txt` | GitHub serves the split for about an hour after the run finishes |

**The hour is a fetching deadline, not a retention limit.** Text fetched into
`ct_step_logs` is ordinary data, readable forever.

## Naming a workflow: path, file name or id

A display name is not a reference. `name:` lives in the workflow file, so the same
file can carry a different name on every branch. Use the path or the id.

## Failure

Only `FAILED_CONCLUSIONS` count. `conclusion = null` is GitHub still materializing
metadata, not a failure.

## Webhooks lower latency; they are not the truth

A delivery can be lost and a run is often created outside this service, so every
run-level read asks GitHub again. When GitHub cannot answer, the service reports
that — it never degrades to a cached answer.
