# Installation

Three shapes: the CLI alone, one container, or two containers with compose.

## From PyPI / source

```bash
pip install rhosocial-github-ci-tracker
```

The image and the CI both run **Python 3.15**. `requires-python` stays at 3.10 for
anyone installing the package; the runtime we build and test is 3.15.

## The image

```
registry.cn-shanghai.aliyuncs.com/vistart_public/citracker:latest
```

- Multi-arch: `linux/amd64`, `linux/arm64`.
- Only the Python runtime, the service code and an entrypoint — no database, no
  certificates, no proxy or pip-mirror configuration.
- Build it yourself: `docker buildx bake --load` for the current architecture,
  `docker buildx bake citracker-all --push` for every platform.

## Ports

| Port | Process | Meaning |
|---|---|---|
| 8000 | `serve all` / `serve rest` | REST API, plain HTTP, container-only by default |
| 18080 | `serve all` / `serve webhook` | GitHub webhook receiver; can terminate TLS |
| 8001 | `serve mcp` | MCP Streamable HTTP, plain HTTP |
