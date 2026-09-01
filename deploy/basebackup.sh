#!/usr/bin/env bash
set -euo pipefail

# Real pg_basebackup-based PHYSICAL backup -- the other half of PITR (spec
# section 9.2, idea.md ~line 6161: "PostgreSQL PITR with WAL archiving,
# 30-day retention"). deploy/backup.sh's pg_dump output is a LOGICAL
# backup: portable and great for "restore this into a fresh database right
# now," but architecturally unable to replay forward to an arbitrary point
# in time between two backups. Point-in-time recovery needs a physical
# base backup (this script) plus the continuously archived WAL segments
# (deploy/docker-compose.yml's postgres service, archive_mode=on) replayed
# forward by deploy/restore_pitr.sh -- see tests/integration/
# test_backup_restore.py for the real drill proving the combination
# actually works, and DECISIONS.md for the full design.
#
# -X none: this base backup deliberately excludes WAL -- recovery relies
# entirely on the archived segments, the standard base-backup + archive
# combination, and the simplest correct one for a single-node deployment.
#
# COMPOSE_FILE and POSTGRES_USER default to the dev stack, same as
# backup.sh and for the same reason -- see that script's own comment.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
OUT_ROOT="${BACKUP_DIR:-$SCRIPT_DIR/../backups}/basebackups"
PG_USER="${POSTGRES_USER:-jobingo}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST_DIR="$OUT_ROOT/$TIMESTAMP"
mkdir -p "$DEST_DIR"

docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_basebackup -U "$PG_USER" -D - -F tar -X none --checkpoint=fast > "$DEST_DIR/base.tar"

echo "Base backup written to $DEST_DIR/base.tar"
