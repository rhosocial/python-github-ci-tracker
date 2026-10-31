# Operations

## Probes

| Path | Question | Check | On failure |
|---|---|---|---|
| `/livez` | is the process alive | none (zero I/O) | restart the container |
| `/readyz` | can this instance serve | one database round trip | drain it, do not restart |
| `/health`, `/healthz` | aliases of `/readyz` | same | same |

Both carry the build stamp (`build.revision`, `build.number`, `built_at`), which is
the one thing that answers "is the deployment the commit I pushed?" — `version`
is a source constant and cannot tell two images apart. A `null` build means the
image was not stamped (locally built), not that it is old.

## Stop, restart, upgrade

| Action | Containers | init runs again | Data |
|---|---|---|---|
| `stop` / `start` | kept | yes, skips when tables exist | unchanged |
| `restart web` | kept | no (`INITDB=skip`) | unchanged |
| `down` → `up -d` | recreated | yes, skips | unchanged (bundled keeps its volume) |
| `down -v` | deleted | yes, creates for real | **data lost** |

Upgrading:

```bash
docker compose --env-file docker/stack.env pull
docker compose --env-file docker/stack.env up -d
docker compose --env-file docker/stack.env ps
curl -k https://127.0.0.1:18080/readyz
```

**A schema change means restarting `web` and `mcp`**: the pool runs server-side
prepared statements, and DDL from another process leaves cached plans that fail
with `cached plan must not change result type` until those connections are
replaced. `up -d` recreates the containers and is not affected.

## Replacing a PAT

1. Edit the token file the compose stack mounts read-only (or the
   `CITRACKER_BOOTSTRAP_PAT` value).
2. Re-run init: `docker compose --env-file docker/stack.env run --rm -e CITRACKER_INITDB=force init`.

Prefer `CITRACKER_BOOTSTRAP_PAT_FILE` over `_PAT`: an inline value is visible in
`docker inspect`, and a leaked token should be revoked at GitHub immediately.

## Legacy / manual mode

```bash
docker compose --env-file docker/stack.env run --rm -e CITRACKER_INITDB=skip init
docker compose exec web rhosocial-github-ci-tracker-db init   # tables only, idempotent
docker compose exec web rhosocial-github-ci-tracker credential pat ensure --name work --token <PAT>
```

## Database maintenance

```bash
docker compose exec web rhosocial-github-ci-tracker-db is-initialized
docker compose exec web rhosocial-github-ci-tracker-db stats
docker compose exec web rhosocial-github-ci-tracker-db usage
docker compose exec web rhosocial-github-ci-tracker-db cleanup --runs-before 90d --logs-before 365d
docker compose exec web rhosocial-github-ci-tracker-db deliveries --unhandled --limit 20
docker compose exec web rhosocial-github-ci-tracker-db cleanup --deliveries-before 30d
docker compose exec web rhosocial-github-ci-tracker-db reset --yes
docker compose exec web rhosocial-github-ci-tracker-db drop --yes
```

Remember: PostgreSQL does not return space to the filesystem on `DELETE` alone.
With the log tables partitioned, dropping a month is one `DROP TABLE` and needs
no vacuum at all.

## Runtime proxy (mainland China only)

The image carries no proxy configuration. A deployment in mainland China injects
it at runtime:

```yaml
services:
  web:
    environment:
      HTTP_PROXY: http://proxy:7897
      HTTPS_PROXY: http://proxy:7897
      NO_PROXY: 127.0.0.1,localhost,::1
```
