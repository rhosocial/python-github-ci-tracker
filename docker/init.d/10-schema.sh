#!/bin/sh
# docker/init.d/10-schema.sh
#
# First-boot operational script: create the database and every table.
#
# This is deployment data, not image logic. docker-compose.yml mounts
# ./docker/init.d at /docker-entrypoint-init.d and the entrypoint runs it once,
# when `rhosocial-github-ci-tracker-db is-initialized` reports no schema.
#
# `db init` is idempotent (CREATE TABLE IF NOT EXISTS) and also creates the
# database when the server does not have it, so re-running by hand or with
# CITRACKER_INITDB=force is safe.
set -eu

echo "citracker init: ensuring the database schema"
rhosocial-github-ci-tracker-db init
