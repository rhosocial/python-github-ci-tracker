# Logs and windows

## Two reads, two scopes

```
get_job_log(job_id)                        the job's merged document (ct_job_logs)
get_job_log(job_id, step=N)                that step's own file (ct_step_logs)
get_job_log(job_id, failed_steps_only=true)   the text of every failed step
search_job_log(job_id, query)              search the merged document
search_job_log(job_id, query, step=N)      search one step's document
```

`search_job_log` and `get_job_log` take the same `step` argument: `source`
(`job` / `step`) in the answer says which scope the line numbers belong to. A step
with no captured log returns a `reason` (`capture_closed` /
`not_a_step_archive` …) — never `no_match`, because "no document" and "no match"
are different answers.

## Windows address, they do not interpret

| Argument | Meaning |
|---|---|
| none | the last 200 lines |
| `tail=N` | the last N lines |
| `from_line` / `to_line` | an explicit inclusive range |
| `grep` | your regex (invalid patterns fall back to a literal) plus `context` |

No test-summary anchoring, no error markers, no trimming of a runner's teardown:
those are properties of one project's output, and a general tracker that guesses
at them is wrong for every other project.

## Reading a step never captures

`get_job_log(step=N)` answers what is stored. To fetch again, call
`get_job(job_id, refresh_logs=true)` — meaningful only while GitHub still serves
the split (before `capture.window_closes_at`).

## Capacity and cleanup

- `db usage` reports what the database holds;
- the log tables are partitioned by month, so `db partitions --drop-before <N>m`
  drops a whole month without locking the table or running a vacuum.
