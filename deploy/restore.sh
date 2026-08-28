#!/usr/bin/env bash
set -euo pipefail

# Restores a pg_dump custom-format backup (see deploy/backup.sh) into the
# docker-compose Postgres. DESTRUCTIVE against its target: drops and
# recreates the target database first, rather than relying on pg_restore's
# own --clean flag (which only drops objects it finds a matching CREATE
# for in the dump, silently leaving behind anything created since the
# backup was taken) -- a real restore drill must guarantee the
# post-restore database contains exactly what's in the dump, nothing else.
#
# Defaults to a SEPARATE database name (jobingo_restore_drill), never the
# live "jobingo" database, so running this script can never be an accident
# that wipes real data -- restoring over the live database is only ever
# done by naming it explicitly as the second argument.
#
# COMPOSE_FILE and POSTGRES_USER default to the dev stack, same as
# backup.sh and for the same reason (an architecture audit caught neither
# script could target production at all) -- see backup.sh's own comment.

if [ $# -lt 1 ]; then
  echo "usage: $0 <backup-file> [target-database]" >&2
  exit 1
fi

BACKUP_FILE="$1"
TARGET_DB="${2:-jobingo_restore_drill}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
PG_USER="${POSTGRES_USER:-jobingo}"

if [ ! -f "$BACKUP_FILE" ]; then
  echo "backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

docker compose -f "$COMPOSE_FILE" exec -T postgres \
  psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$TARGET_DB\" WITH (FORCE)"
docker compose -f "$COMPOSE_FILE" exec -T postgres \
  psql -U "$PG_USER" -d postgres -c "CREATE DATABASE \"$TARGET_DB\" OWNER $PG_USER"
docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_restore -U "$PG_USER" -d "$TARGET_DB" < "$BACKUP_FILE"

echo "Restored $BACKUP_FILE into database '$TARGET_DB'"
