# python-ci-tracker

[![CI](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml/badge.svg)](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.15-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-en_US_%2F_zh__CN-green.svg)](docs/README.md)

**A self-hosted service that answers "what happened in CI" — over MCP, REST and a CLI.**

It reads GitHub Actions runs, jobs, steps and logs; it does not poll in the
background, does not keep a central token pool, and never treats a cached run as
the truth when GitHub can still answer.

简体中文 | [简体中文说明](README.zh-CN.md) | [English](README.md)

---

## What you get

| | |
|---|---|
| **Ask once, in any protocol** | `ci_status`, `find_runs`, `get_run`, `get_job`, `get_job_log`, `search_job_log`, `wait_for_run`, `trigger_workflow` — over MCP (Streamable HTTP), REST and a CLI. One flow, three shapes; a model or a script uses the same answers. |
| **Three layers, one step each** | `get_run` names the job that failed, `get_job` names the step, `get_job_log` returns its text. No call answers two layers, so no caller has to know which one it is looking at. |
| **Step-level logs, kept** | GitHub splits a run into per-step files for about an hour; after that the same endpoint returns one merged document. The service captures the split while it exists, stores it as ordinary rows, and reads it forever. Missed capture is reported honestly — never replaced by another step's text. |
| **Windows that address, not interpret** | `tail`, an explicit line range, or your own regex. No guessing at test summaries or teardown output, because a general CI tracker cannot know what any project prints. |
| **Trigger and wait, without burning minutes** | `trigger_workflow` dispatches on request; `if_absent=true` answers "this commit already ran" without starting a second run. `wait_for_run` is a bounded long poll whose timeout comes from the workflow's own history. |
| **PostgreSQL, and it says so** | Log tables are partitioned by month so old data is one `DROP TABLE`; the schema is the service's own renderer; the test suite runs against a real server. |

## Why you would use it

- **For an agent**: one call answers a whole question and names the next call. A
  failing job tells you which step failed and whether its log is on disk — no
  shell scrolling, no second service to stub.
- **For a team with a matrix**: 77-job run, one `get_run`; the failing step's
  log comes back in one call, windowed to 200 lines by default.
- **For someone on a free-tier database**: capacity is visible (`db usage`), and
  pruning is a partition drop rather than a `VACUUM` that locks the table.

## Where the details live

- [Documentation (English)](docs/en_US/README.md) — deployment, the API, the
  CLI, development
- [文档（简体中文）](docs/zh_CN/README.md)

## License

[Apache License 2.0](LICENSE).
