#!/usr/bin/env bash
set -euo pipefail

# Real point-in-time recovery: replays deploy/basebackup.sh's physical base
# backup forward through the archived WAL segments (deploy/docker-compose
# .yml's postgres service, archive_mode=on) to an exact target timestamp --
# the capability deploy/restore.sh's pg_dump/pg_restore path architecturally
# cannot provide (a logical dump is a single instant, not a replayable
# stream). See deploy/basebackup.sh's own comment and DECISIONS.md for the
# full design.
#
# Unlike restore.sh (a separate DATABASE on the SAME running server), a
# physical PITR restore needs its own separate DATA DIRECTORY and Postgres
# PROCESS -- this spins up a genuinely separate, throwaway container via
# plain `docker run --rm`, never touching the live postgres service. Same
# "never risk the real thing" principle as restore.sh, one level down the
# stack.
#
# usage: restore_pitr.sh <base_backup.tar> <target_time> <host_port> [container_name]
#
# <target_time> is any timestamp string Postgres's recovery_target_time GUC
# accepts (e.g. the output of `SELECT clock_timestamp()`). recovery_target_
# action=promote below means the container comes all the way up read-write
# once it reaches that point, ready for a caller to query immediately --
# no separate "promote" step needed.
#
# Leaves the container RUNNING on success (started with --rm, so `docker
# stop <container_name>` both stops and removes it) -- teardown is the
# caller's job, the same division of responsibility backup.sh/restore.sh
# already use (they don't clean up their own output either).

if [ $# -lt 3 ]; then
  echo "usage: $0 <base_backup.tar> <target_time> <host_port> [container_name]" >&2
  exit 1
fi

BASE_BACKUP="$1"
TARGET_TIME="$2"
HOST_PORT="$3"
CONTAINER_NAME="${4:-jobingo-pitr-drill}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAL_ARCHIVE_DIR="${BACKUP_DIR:-$SCRIPT_DIR/../backups}/wal_archive"

if [ ! -f "$BASE_BACKUP" ]; then
  echo "base backup not found: $BASE_BACKUP" >&2
  exit 1
fi
if [ ! -d "$WAL_ARCHIVE_DIR" ]; then
  echo "WAL archive directory not found: $WAL_ARCHIVE_DIR" >&2
  exit 1
fi

PGDATA_DIR="$(mktemp -d)"
trap 'rm -rf "$PGDATA_DIR"' EXIT

# -p/--preserve-permissions: pg_basebackup's tar preserves PGDATA's real
# 0700 mode on every entry -- Postgres refuses to start against a data
# directory it considers group/world-accessible, and plain `tar` extraction
# as a non-root user otherwise applies the process umask on top of what's
# stored in the archive instead of restoring it exactly.
tar -xf "$BASE_BACKUP" -p -C "$PGDATA_DIR"

cat >> "$PGDATA_DIR/postgresql.auto.conf" <<EOF
restore_command = 'cp /wal_archive/%f %p'
recovery_target_time = '$TARGET_TIME'
recovery_target_action = 'promote'
EOF
touch "$PGDATA_DIR/recovery.signal"

docker run -d --rm \
  --name "$CONTAINER_NAME" \
  --user "$(id -u):$(id -g)" \
  -v "$PGDATA_DIR:/var/lib/postgresql/data" \
  -v "$WAL_ARCHIVE_DIR:/wal_archive:ro" \
  -p "$HOST_PORT:5432" \
  postgres:15 >/dev/null

# --user (not root) plus a pre-populated PGDATA means the official image's
# entrypoint detects the existing cluster (PG_VERSION present) and skips
# initdb entirely, going straight to starting Postgres against it -- no
# POSTGRES_PASSWORD or first-boot init needed.

READY=""
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER_NAME" pg_isready -U jobingo >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ -z "$READY" ]; then
  echo "recovered instance never became ready within 60s; container logs:" >&2
  docker logs "$CONTAINER_NAME" >&2 || true
  docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
  exit 1
fi

# PGDATA_DIR is bind-mounted into the still-running container -- don't
# clean it up on the happy path, only once the caller stops the container.
trap - EXIT

echo "Recovered instance ready: container=$CONTAINER_NAME port=$HOST_PORT pgdata=$PGDATA_DIR"
