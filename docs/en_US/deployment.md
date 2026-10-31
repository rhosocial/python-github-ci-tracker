# Deployment

## Shape one: web + mcp, external PostgreSQL

```bash
cp docker/stack.env.example docker/stack.env
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/
docker compose --env-file docker/stack.env up -d
curl -k https://127.0.0.1:18080/readyz
```

`docker-compose.yml` defines three services: `init` (once, runs `./docker/init.d`
and exits), `web` (`serve all`, REST 8000 + webhook 18080) and `mcp`
(`serve mcp`, 8001). `web` and `mcp` start only after `init` succeeded.

## Shape two: bundled `postgres:18`

```bash
docker compose -f docker-compose.yml -f docker-compose.bundled.yml --env-file docker/stack.env up -d
```

One database volume; `down` keeps it, `down -v` deletes it.

## First boot (`init.d`)

`10-schema.sh` creates the database and the tables (`CREATE DATABASE` +
`CREATE TABLE IF NOT EXISTS`), `20-pat.sh` ensures the credential. Both are
idempotent; **initialisation never clears existing data**.

Startup order is guaranteed by `depends_on`; the database does not have to sit
next to the application.

## TLS

TLS material lives outside the image: mount a directory at `/certs` and point
`CITRACKER_WEBHOOK_SSL_CERTFILE` / `_KEYFILE` at the two files inside it. GitHub
requires an HTTPS callback for webhooks.
