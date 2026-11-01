# CITracker service image: the Python runtime only -- no database, no
# certificates, no secrets. Pair it with any PostgreSQL (see
# docker-compose.yml for postgres:18) and mount TLS material from outside.
#
# No `# syntax` directive on purpose: the engine's built-in frontend covers
# everything used here, and pinning docker/dockerfile:1 would pull from
# docker.io -- unreachable from this network.
#
# Nothing proxy-related belongs in this image: no HTTP(S)_PROXY, no pip index
# or mirror override, no build-arg plumbing for one. The image is published to
# users worldwide, and a proxy is a runtime choice for deployments in mainland
# China -- pass it in the deployment environment, never here.
#
# Multi-stage by design: the builder installs the project and its siblings into
# a virtualenv, and the runtime copies that venv and nothing else -- no
# compilers, no caches, no credentials. The builder carries build-essential
# packages so every architecture can build, including the ones where a
# dependency (psycopg) has no binary wheel and must compile; they never reach
# the runtime layer.
#
# Dependencies come from the sibling repositories' release branches -- the line
# this project is verified against, and the same refs CI checks out. All three
# siblings are public, so no token is needed at build time. Once those versions
# are published, replace the git URLs with pins.
#
# Architectures mirror the python:3.15-slim upstream list (see docker-bake.hcl):
# amd64, arm64, armv7, armv5, 386, ppc64le, riscv64, s390x.
#
# The base image is the upstream python:3.15-slim; override the argument to
# build on a different image.

ARG PYTHON_IMAGE=python:3.15-slim

FROM ${PYTHON_IMAGE} AS builder

ARG TARGETPLATFORM
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# psycopg ships binary wheels for amd64 and arm64, the two platforms this
# project publishes; the remaining ones compile against libpq, which needs a
# toolchain in the builder stage only -- never the runtime layer. git is always
# required: the sibling dependencies install from git URLs, and pip shells out
# to the git binary for those.
RUN set -eux; \
    case "$TARGETPLATFORM" in \
      linux/amd64 | linux/arm64) \
        apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
        && rm -rf /var/lib/apt/lists/* ;; \
      *) \
        apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
        gcc libpq-dev && rm -rf /var/lib/apt/lists/* ;; \
    esac

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Siblings first, exactly as the service documents and CI does it: this
# project's requirements resolve against them, and installing it first would
# send pip to PyPI for versions that are not published there.
RUN pip install "git+https://github.com/rhosocial/python-activerecord.git@release/v1.0.0.dev30" \
    && pip install "git+https://github.com/rhosocial/python-activerecord-postgres.git@release/v1.0.0.dev17" \
    && pip install "git+https://github.com/rhosocial/python-mcpserver.git@main"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .


FROM ${PYTHON_IMAGE} AS runtime

# The build stamp. ``version`` is a constant in the source tree, so it cannot
# tell two images apart; these three arguments are what the publishing workflow
# fills in and what ``/readyz``, ``/livez`` and ``ci_status`` report back. They
# are arguments rather than labels because a label is image metadata that the
# process cannot read -- the running service needs them as environment.
ARG GIT_REVISION=""
ARG GIT_BUILD_NUMBER=""
ARG GIT_BUILT_AT=""

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CITRACKER_BUILD_REVISION=${GIT_REVISION} \
    CITRACKER_BUILD_NUMBER=${GIT_BUILD_NUMBER} \
    CITRACKER_BUILD_BUILT_AT=${GIT_BUILT_AT}

LABEL org.opencontainers.image.revision=${GIT_REVISION} \
      org.opencontainers.image.created=${GIT_BUILT_AT} \
      org.opencontainers.image.title="citracker"

COPY --from=builder /opt/venv /opt/venv

# Unprivileged and home-owned: the service writes nothing to its own
# filesystem -- state lives in PostgreSQL and logs go to stdout/stderr.
RUN useradd --create-home --uid 10001 appuser
USER appuser
WORKDIR /home/appuser

# Every listener the service can start. The default command starts the REST
# (8000) and webhook (18080) pair; `serve mcp` adds 8001 in its own container.
# REST and MCP are plain HTTP and are expected to sit behind a TLS-terminating
# proxy when exposed directly; the webhook receiver terminates TLS itself.
EXPOSE 8000 8001 18080

# TLS material lives outside the image by convention: mount a directory at
# /certs and point CITRACKER_WEBHOOK_SSL_CERTFILE / _KEYFILE at the two files
# inside it. Nothing under /certs is created, copied, or owned by this image.

# --chmod, not a chmod step afterwards: the entrypoint must be executable the
# moment it lands, because it is the container's ENTRYPOINT.
COPY --chown=appuser:appuser --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["rhosocial-github-ci-tracker", "serve", "all"]
