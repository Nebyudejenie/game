# Disaster Recovery

The mechanism described here is real and tested (`tests/integration/
test_backup_restore.py` proves the dump→restore and basebackup→PITR
round trips actually work, byte-for-byte). **Whether it is actually
running on a schedule in production today is unverified** — this
document was written without access to the production host (see `docs/
PRODUCTION_READINESS.md`'s own note on why). Confirm the "Is this
actually running?" checklist at the bottom before trusting any RPO/RTO
number here as a live guarantee rather than a capability.

## What's real

| Piece | Script | What it does |
|---|---|---|
| Continuous WAL archiving | Postgres itself (`archive_mode=on`, `archive_command` set in both compose files) | Every completed WAL segment is copied to `backups/wal_archive/` **continuously, automatically**, by Postgres's own process — not something `backup.sh` or a cron job triggers. This is already active the moment the `postgres` container starts with this config, in both dev and prod compose files. |
| Logical backup | `deploy/backup.sh` | `pg_dump -F custom` (compressed, `pg_restore`-compatible, supports selective/parallel restore) → `backups/<db>-<UTC timestamp>.dump`. One-shot; needs an external scheduler. |
| Physical base backup | `deploy/basebackup.sh` | `pg_basebackup -F tar -X none` → `backups/basebackups/<timestamp>/base.tar` — the "anchor point" a PITR restore replays WAL forward from. One-shot; needs an external scheduler. |
| Point-in-time restore | `deploy/restore_pitr.sh` | Extracts a basebackup tar, sets `restore_command`/`recovery_target_time`/`recovery_target_action=promote`, replays archived WAL in a throwaway `docker run --rm` Postgres container. |
| Logical restore | `deploy/restore.sh` | `pg_restore` into a **named target database** (defaults to `jobingo_restore_drill`, never the real `jobingo` database unless explicitly told to) — safe to run against production's own dump without touching the live database. |
| Retention | `deploy/prune_wal_archive.sh` | Deletes WAL segments and basebackup directories older than `<days>` (default 30) — spec's own documented 30-day retention window. One-shot; needs an external scheduler. |

## RPO / RTO — what the mechanism is *capable* of

**RPO (Recovery Point Objective) — how much data could be lost:**
Because WAL archiving is continuous (a Postgres-native process, not
dependent on any cron job), a point-in-time restore's RPO is bounded by
how current the WAL archive is, not by how recently a basebackup was
taken — in principle, near-zero data loss up to the last archived WAL
segment. **This number is only real if WAL archiving is actually
confirmed working in production right now** (see checklist below) — a
misconfigured or silently-failing `archive_command` would mean this
guarantee doesn't actually hold, with no visible symptom until the day
someone needs it.

**RTO (Recovery Time Objective) — how long recovery takes:** Bounded by
how much WAL has to be replayed forward from the most recent basebackup.
**If no basebackup has ever actually been taken**, a PITR restore has
nothing to start replaying from at all — WAL segments alone, with no
anchor, cannot restore a database. This is the single most important
unverified fact in this whole document (see checklist).

**Retention window:** 30 days by default (`prune_wal_archive.sh`) — a
disaster discovered more than 30 days after it happened cannot be
recovered via PITR (the logical `backup.sh` dumps, if scheduled and
retained separately, are a second, independent recovery point not
subject to this same 30-day WAL-archive pruning).

## Recovery procedures

### Logical restore (schema + data, to a fresh/target database)

```bash
COMPOSE_FILE=deploy/docker-compose.prod.yml POSTGRES_USER=<real user> \
  ./deploy/restore.sh <dump-file> <target-db-name>
```

Never point `<target-db-name>` at the live `jobingo` database on a
running production instance — restore to a fresh name, verify it, then
make *that* the decision point for cutover (stop the app, rename
databases, restart), not a blind overwrite.

### Point-in-time restore (replay to an exact moment)

```bash
./deploy/restore_pitr.sh <basebackup-timestamp-dir> "<recovery_target_time>"
```

Spins up a throwaway, isolated Postgres container — inspect the restored
state there before deciding to promote it to production traffic.

## Is this actually running? (confirm before trusting any number above)

- [ ] SSH to the production host, run `docker compose -f
      deploy/docker-compose.prod.yml logs postgres | grep -i archive` —
      confirm WAL segments are actually being archived, not just
      configured to be.
- [ ] `ls -la <production>/backups/wal_archive/` — confirm recent files
      exist, not an empty directory.
- [ ] `ls -la <production>/backups/` and `<production>/backups/
      basebackups/` — confirm at least one logical dump and one physical
      basebackup actually exist, and check their timestamps against
      "recent enough to matter."
- [ ] Confirm what's actually invoking `backup.sh`/`basebackup.sh`/
      `prune_wal_archive.sh` on a schedule — `crontab -l`, `systemctl
      list-timers`, or equivalent, on the production host itself. If
      nothing appears, **no scheduled backup exists today** regardless of
      what this document describes as possible.
- [ ] Run one real restore drill against a *real* production backup file
      (never against production itself) and confirm the restored data
      looks right — proves the specific files being produced today are
      actually restorable, not just that the script mechanism works
      against a fresh test database.

Until every box above is checked on the real production host, treat this
document as "the tooling exists and is tested" — not as "disaster
recovery is armed."
