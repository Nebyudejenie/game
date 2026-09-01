#!/usr/bin/env bash
set -euo pipefail

# Spec section 9.2's "30-day retention" (idea.md ~line 6161) for the WAL
# archive + physical base backups deploy/basebackup.sh/restore_pitr.sh use
# for PITR. Deletes archived WAL segments (backups/wal_archive/*) and base
# backup directories (backups/basebackups/*) whose mtime is older than
# <days> (default 30). A base backup older than the retention window is
# useless anyway once the WAL segments after it have been pruned -- nothing
# left to replay forward from it -- so both prune on the same cutoff.
#
# Meant to run on the same schedule as packages/core/reconcile_job.py (see
# README.md's "Nightly ledger reconciliation" section for why the actual
# cron/systemd-timer line is a deploy-time step, not committed here: the
# real checkout path on the server is only known once someone registers
# it, the same reasoning that section's own note already makes).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ROOT="${BACKUP_DIR:-$SCRIPT_DIR/../backups}"
DAYS="${1:-30}"

WAL_DIR="$BACKUP_ROOT/wal_archive"
BASEBACKUP_DIR="$BACKUP_ROOT/basebackups"

DELETED=0

if [ -d "$WAL_DIR" ]; then
  while IFS= read -r -d '' f; do
    rm -f "$f"
    DELETED=$((DELETED + 1))
  done < <(find "$WAL_DIR" -maxdepth 1 -type f -mtime "+$DAYS" -print0)
fi

if [ -d "$BASEBACKUP_DIR" ]; then
  while IFS= read -r -d '' d; do
    rm -rf "$d"
    DELETED=$((DELETED + 1))
  done < <(find "$BASEBACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$DAYS" -print0)
fi

echo "Pruned $DELETED item(s) older than $DAYS day(s) from $BACKUP_ROOT"
