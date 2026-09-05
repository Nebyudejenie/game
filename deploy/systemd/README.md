# Backup scheduling — systemd units

Real, ready-to-install systemd timer units closing
`docs/LAUNCH_BLOCKERS.md`'s LB-B2 (no backup schedule wired anywhere).
These call the existing, tested scripts (`deploy/backup.sh`,
`deploy/basebackup.sh`, `deploy/prune_wal_archive.sh`) — no new backup
mechanism, just the missing scheduler around what already works.

## What's here

| Unit pair | Runs | Cadence |
|---|---|---|
| `jobingo-backup.{service,timer}` | `deploy/backup.sh` (logical `pg_dump`) | Daily, 02:00 |
| `jobingo-basebackup.{service,timer}` | `deploy/basebackup.sh` (physical base backup for PITR) | Weekly, Sunday 03:00 |
| `jobingo-prune-wal.{service,timer}` | `deploy/prune_wal_archive.sh 30` (30-day retention) | Daily, 04:00 |

## Before installing: one placeholder to fix

Every `.service` file has `WorkingDirectory=/home/cosmic/game` and a
matching path in `Environment=COMPOSE_FILE=...` and `ExecStart=...` —
this is this repo's own known production deploy path
(`docs/reference_deployment_target.md`-equivalent: `cosmic@192.168.1.173:
/home/cosmic/game`). **Confirm this is still correct on the real host
before installing** — if the checkout ever moves, update all three
`.service` files to match, the same as any other deploy-path-dependent
script in this repo.

## Install (on the production host, as a user with sudo)

```bash
sudo cp deploy/systemd/jobingo-*.service deploy/systemd/jobingo-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobingo-backup.timer
sudo systemctl enable --now jobingo-basebackup.timer
sudo systemctl enable --now jobingo-prune-wal.timer
```

## Verify

```bash
systemctl list-timers | grep jobingo
# Expect three rows with a real NEXT time, not "n/a".

# Run one manually right now rather than waiting for the schedule, to
# confirm it actually works end to end before trusting the timer alone:
sudo systemctl start jobingo-backup.service
systemctl status jobingo-backup.service   # should show "Succeeded"
ls -la backups/   # a new dump file should exist with a fresh timestamp
```

This is exactly `docs/LAUNCH_BLOCKERS.md` LB-B2's own verification
command (`systemctl list-timers | grep jobingo-backup`) — running it
after this install is what actually closes that item, not just having
these files exist in the repo.

## After this is running

`docs/DISASTER_RECOVERY.md`'s "Is this actually running?" checklist and
`docs/DISASTER_RECOVERY_DRILL.md`'s scenario 1 (DB corruption) both
currently say RPO/RTO are unconfirmed pending a real schedule — once
these timers have run for a few days, re-check that checklist for real,
and perform a real restore drill (LB-B3) against one of the backups these
now produce.
