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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
OUT_DIR="${BACKUP_DIR:-$SCRIPT_DIR/../backups}"
DB_NAME="${1:-jobingo}"

mkdir -p "$OUT_DIR"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="$OUT_DIR/${DB_NAME}-${TIMESTAMP}.dump"

docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_dump -U jobingo -d "$DB_NAME" -F custom > "$OUT_FILE"

echo "Backup written to $OUT_FILE"
