# Configuration

Every setting comes from an environment variable; the service has no config file.

| Variable | Default | Meaning |
|---|---|---|
| `CITRACKER_DB_HOST/PORT` | `192.168.1.3:16689` (in compose: `postgres:5432`) | PostgreSQL coordinates |
| `CITRACKER_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | database account |
| `CITRACKER_DB_SSLMODE` | `prefer` | libpq sslmode |
| `CITRACKER_POOL_MIN/MAX` | `1` / `10` | pool; **each request holds one connection**, and `wait_for_run` holds one for its whole wait |
| `CITRACKER_API_KEY` | empty | Bearer for REST / MCP; empty means no auth (warns at startup) |
| `CITRACKER_REST_HOST/PORT` | `0.0.0.0:8000` | REST listener |
| `CITRACKER_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP listener |
| `CITRACKER_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook listener |
| `CITRACKER_WEBHOOK_SECRET` | empty | HMAC secret, must match the GitHub webhook |
| `CITRACKER_WEBHOOK_URL` | empty | default callback URL |
| `CITRACKER_PUBLIC_BASE_URL` | empty | prefix for absolute action URLs |
| `CITRACKER_WEBHOOK_SSL_CERTFILE/KEYFILE` | empty | webhook TLS; empty means plain HTTP |
| `CITRACKER_DEFAULT_EVENTS` | `workflow_run,workflow_job` | subscribed events |
| `CITRACKER_WAIT_MAX_SECONDS` | `300` | ceiling for one `wait_for_run` call |
| `CITRACKER_LOG_CAPTURE` | `1` | capture step logs at all; `0` falls back to merged job logs |
| `CITRACKER_LOG_CAPTURE_WINDOW_MINUTES` | `60` | how long after completion a capture is still attempted (measured: GitHub stops the split between 56 and 70 minutes) |
| `CITRACKER_LOG_CAPTURE_RETRIES` | `6` | attempts while the archive is still filling |
| `CITRACKER_LOG_CAPTURE_RETRY_DELAY_SECONDS` | `60` | seconds between them |
| `CITRACKER_INITDB` | `auto` | `auto` / `force` / `skip` |
| `CITRACKER_INITDB_DIR` | `/docker-entrypoint-init.d` | where the init scripts live |
| `CITRACKER_LEGACY` | `0` | `1` = `CITRACKER_INITDB=skip` |
| `CITRACKER_BOOTSTRAP_PAT` | empty | PAT injected on first boot |
| `CITRACKER_BOOTSTRAP_PAT_FILE` | empty | read the PAT from a file, so it never appears in `docker inspect` |
| `CITRACKER_BOOTSTRAP_PAT_NAME` | `default` | credential alias |
| `CITRACKER_BOOTSTRAP_PROBE_REPO` | empty | `owner/repo` used to verify Actions write access |
| `CITRACKER_IMAGE` | published image | the image compose uses |
| `CITRACKER_WEBHOOK_PORT_HOST` | `18080` | host-published webhook port |
| `CITRACKER_MCP_PORT_HOST` | `8001` | host-published MCP port |
| `CITRACKER_BUILD_REVISION` | empty | written by the image build; reported by `/readyz` and `ci_status` |
| `CITRACKER_BUILD_NUMBER` | empty | the publishing workflow's run number |
| `CITRACKER_BUILD_BUILT_AT` | empty | build timestamp, ISO 8601 UTC |
