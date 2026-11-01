#!/bin/sh
# CITracker container entrypoint.
#
# The image carries no operational bootstrap: schema creation and credential
# provisioning are deployment concerns, so they live in scripts under
# CITRACKER_INITDB_DIR (default /docker-entrypoint-init.d), the same mechanism
# the official database images use. On the first boot the entrypoint runs every
# *.sh there in lexical order; once the schema is in place the scripts are
# skipped, and a deployment that manages its own schema opts out entirely.
#
# Modes (CITRACKER_INITDB):
#
#   auto   (default) run the scripts when `rhosocial-github-ci-tracker-db
#          is-initialized` reports that the schema is absent
#   force  run the scripts regardless; use it after `db reset`, or to reconcile
#          a credential added after the first boot
#   skip   never run the scripts; the deployment owns schema and credentials
#
# CITRACKER_LEGACY=1 is kept as a spelling for skip.
#
# The scripts receive the container environment (database coordinates, PAT,
# ...) and are mounted read-only by compose: nothing here is part of the image.
# They run with /bin/sh, so an executable bit is not required -- which also
# makes a Windows bind mount work.
#
# Logs go to stdout/stderr by design: `docker logs` is the log reader.
set -eu

INITDB_DIR="${CITRACKER_INITDB_DIR:-/docker-entrypoint-init.d}"
INITDB_MODE="${CITRACKER_INITDB:-auto}"

log() {
    echo "citracker: $*"
}

# Accept the same spellings as the Python settings helper, so a value copied
# from .env.example means the same thing in the entrypoint.
is_true() {
    case "${1:-0}" in
        1 | true | TRUE | yes | YES | on | ON) return 0 ;;
        *) return 1 ;;
    esac
}

# Decide whether this boot is an initialization boot. Returns 0 to run the
# scripts, 1 to skip them, and 2 on an unusable mode -- a typo must not silently
# leave a fresh database uninitialized.
should_initialize() {
    case "$INITDB_MODE" in
        skip | SKIP | Skip | none | NONE | None)
            log "CITRACKER_INITDB=$INITDB_MODE: init scripts disabled"
            return 1
            ;;
        force | FORCE | Force | always | ALWAYS | Always)
            log "CITRACKER_INITDB=$INITDB_MODE: running init scripts"
            return 0
            ;;
        auto | AUTO | Auto | "")
            ;;
        *)
            log "invalid CITRACKER_INITDB='$INITDB_MODE'; use auto, force or skip"
            return 2
            ;;
    esac

    if rhosocial-github-ci-tracker-db is-initialized; then
        log "schema already initialized; skipping init scripts"
        return 1
    fi
    log "schema absent; running init scripts"
    return 0
}

# Run every *.sh in lexical order and stop at the first failure. An empty or
# missing directory is an error: on a first boot it means nothing would create
# the schema, so starting the service would only produce a crash loop. The
# explicit skip mode is the way to say "handled elsewhere".
run_init_scripts() {
    if [ ! -d "$INITDB_DIR" ]; then
        log "no init script directory at $INITDB_DIR"
        log "mount one there, or set CITRACKER_INITDB=skip to manage the schema yourself"
        return 1
    fi

    found=0
    for script in "$INITDB_DIR"/*.sh; do
        [ -f "$script" ] || continue
        found=1
        log "running $(basename "$script")"
        /bin/sh "$script"
    done

    if [ "$found" -eq 0 ]; then
        log "no *.sh scripts in $INITDB_DIR"
        log "mount one there, or set CITRACKER_INITDB=skip to manage the schema yourself"
        return 1
    fi

    log "init scripts completed"
}

if is_true "${CITRACKER_LEGACY:-0}"; then
    log "legacy mode: init scripts disabled"
    exec "$@"
fi

status=0
should_initialize || status=$?
if [ "$status" -eq 2 ]; then
    exit 1
elif [ "$status" -eq 0 ]; then
    run_init_scripts
fi

exec "$@"
