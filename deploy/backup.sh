#!/usr/bin/env bash
set -euo pipefail

# Real pg_dump-based backup of the docker-compose-deployed Postgres, in
# custom format (pg_restore-compatible, compressed, supports selective and
# parallel restore) -- see deploy/restore.sh for the matching restore path
# and tests/integration/test_backup_restore.py for a real drill that proves
# a dump from this script actually restores byte-for-byte.
#
# This only performs one backup; wiring a schedule (cron, a systemd timer,
# a k8s CronJob) around it is a deployment-time decision, the same
# deliberate scope boundary every other job/worker in this codebase draws.
#
# COMPOSE_FILE and POSTGRES_USER default to the dev stack (docker-compose.yml,
# user "jobingo") -- an architecture audit caught that this hardcoded the
# dev compose file with no way to target production at all, so
# COMPOSE_FILE=deploy/docker-compose.prod.yml POSTGRES_USER=... is how an
# operator actually points this at the real database; the defaults keep
# tests/integration/test_backup_restore.py's own real drill working
# unchanged.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
OUT_DIR="${BACKUP_DIR:-$SCRIPT_DIR/../backups}"
DB_NAME="${1:-jobingo}"
PG_USER="${POSTGRES_USER:-jobingo}"

mkdir -p "$OUT_DIR"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="$OUT_DIR/${DB_NAME}-${TIMESTAMP}.dump"

docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_dump -U "$PG_USER" -d "$DB_NAME" -F custom > "$OUT_FILE"

echo "Backup written to $OUT_FILE"
