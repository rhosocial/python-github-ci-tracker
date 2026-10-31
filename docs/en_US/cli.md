# The CLI

Two entry points, `rhosocial-github-ci-tracker` and
`rhosocial-github-ci-tracker-db`. The global `--format` (`text` / `json`) and
`--verbose` flags go **before** the subcommand.

```bash
rhosocial-github-ci-tracker --format json status            # ci_status from the shell
rhosocial-github-ci-tracker runs --name owner/repo         # recent runs
rhosocial-github-ci-tracker latest --name owner/repo       # the newest run
rhosocial-github-ci-tracker jobs --run-id <id> --step 3    # a job's steps
rhosocial-github-ci-tracker logs --job-id <id> --step 3    # a step log
rhosocial-github-ci-tracker trigger --workflow ci.yml --ref main --name owner/repo
rhosocial-github-ci-tracker repo subscribe --name owner/repo --credential pat/work
```

## Database maintenance

```bash
rhosocial-github-ci-tracker-db is-initialized
rhosocial-github-ci-tracker-db init
rhosocial-github-ci-tracker-db stats
rhosocial-github-ci-tracker-db usage
rhosocial-github-ci-tracker-db cleanup --runs-before 90d --logs-before 365d
rhosocial-github-ci-tracker-db deliveries --unhandled --limit 20
```
