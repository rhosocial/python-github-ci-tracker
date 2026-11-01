#!/bin/sh
# docker/init.d/20-pat.sh
#
# First-boot operational script: register or rotate the fine-grained PAT.
#
# This is deployment data, not image logic. docker-compose.yml mounts
# ./docker/init.d at /docker-entrypoint-init.d and passes these variables from
# docker/stack.env:
#
#   CITRACKER_BOOTSTRAP_PAT          the token itself
#   CITRACKER_BOOTSTRAP_PAT_FILE     a file holding the token (preferred)
#   CITRACKER_BOOTSTRAP_PAT_NAME     credential alias, default `default`
#   CITRACKER_BOOTSTRAP_PROBE_REPO   owner/repo used to verify Actions write
#
# With neither PAT nor PAT_FILE set the script is a no-op, so a deployment that
# supplies credentials another way still starts cleanly.
#
# `credential pat ensure` verifies the token with GitHub before writing: a fresh
# database gets a new row, an existing one is rotated in place (id and repository
# bindings preserved). --token-file / --token-stdin keep the value off the
# process list, and the token is never echoed.
set -eu

name="${CITRACKER_BOOTSTRAP_PAT_NAME:-default}"
probe="${CITRACKER_BOOTSTRAP_PROBE_REPO:-}"
file="${CITRACKER_BOOTSTRAP_PAT_FILE:-}"
token="${CITRACKER_BOOTSTRAP_PAT:-}"

if [ -z "$file" ] && [ -z "$token" ]; then
    echo "citracker init: no PAT configured; leaving credentials untouched"
    exit 0
fi

echo "citracker init: ensuring credential pat/$name"
set -- rhosocial-github-ci-tracker credential pat ensure --name "$name"
if [ -n "$probe" ]; then
    set -- "$@" --probe-repo "$probe"
fi

if [ -n "$file" ]; then
    set -- "$@" --token-file "$file"
    "$@"
else
    printf '%s\n' "$token" | "$@" --token-stdin
fi
