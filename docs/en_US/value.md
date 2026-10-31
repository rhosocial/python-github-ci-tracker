# What it saves

Three different things, and the third is the one most easily underestimated.

| | Saved by |
|---|---|
| **Context** — a 4,000-line job log becomes `grep` plus a few lines of context; a 12-job matrix becomes a 12-line roster | `get_run`, `get_job`, `search_job_log`, the `tail`/`from_line`/`next_to_line` window |
| **Wall time** — waiting becomes one bounded call; cancelling and de-duplicating stops minutes you would otherwise spend | `wait_for_run`, `trigger_workflow(if_absent=true)`, `cancel_run`, `typical_duration_seconds` |
| **Wrong conclusions** — "attempt 1 failed, attempt 2 passed" instead of "this backend is flaky" | `attempt`/`is_latest`, `has_log` + `reason`, `ci_status.health.alerts` |

The first two only make you slower without them. The third makes you write a
confident, wrong report — and that report is what other people then act on.

## An operator's session, in their own numbers

One user measured a working session and reported (their estimate, not a
benchmark):

| Question | Without the service | With it |
|---|---|---|
| why did the Oracle job fail | 4,369 log lines of one job | `grep: "DPY-6005"` → 19 line numbers + context |
| which of 12 jobs went red | 12 logs, ~48,000 lines | `get_run` → the roster |
| are these ten repositories green | ten reads | `recent_runs`, one call |

Their sharpest point is not the ratio: **a full matrix log does not fit in a
context window at all**, so the choice is not "cheaper" but "possible". They also
reported saving roughly 50–60 runner-minutes in one day from `cancel_run` and
`if_absent`, and three cases where a field prevented a wrong conclusion rather
than a wasted minute.

The honest side of the same ledger, in their report and ours:

- every call costs schema + JSON, roughly a few hundred tokens *before* any
  content — "is this run green?" is not free;
- a self-hosted service must be identifiable by build, or every evaluation may
  be made against the wrong version (`build.revision` exists for this reason);
- a client that cached the tool catalogue sees the old signature after the server
  changed, so "the server changed" and "I can see the change" are separated by a
  reconnect.

## What we saw using it on itself

Working on this service, through its own tools:

- a 77-job matrix came back as one roster; the failing step's 443-line log came
  back in one windowed read;
- `get_job_log(step=N)` on a run from six days earlier answered `capture_closed`
  rather than an empty document — which is what sent us to fetch the raw archive
  and measure the switch from per-step files to a merged one at between 56 and
  70 minutes;
- `ci_status.health.alerts` listed the run ids whose webhook deliveries had
  failed, so the repair was one pass of `get_run` instead of an investigation;
- querying a 24-branch repository exposed the workflow display name drifting
  between branches — a defect we would not have seen by reading our own
  repository.
