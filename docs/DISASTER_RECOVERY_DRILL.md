# Disaster Recovery Drill — Scenario Runbook

`docs/DISASTER_RECOVERY.md` documents the *mechanism* (backup/restore/
PITR scripts, real and tested). This document walks through 7 concrete
failure scenarios, each with the actual commands to run and — where the
underlying mechanism genuinely determines a number — a derived RPO/RTO.
Where no real schedule/config exists yet to derive a number from, that
field says so explicitly rather than inventing one; several scenarios
stay blocked on `docs/LAUNCH_BLOCKERS.md`'s LB-B2 (backup schedule) for
exactly this reason.

A drill is only real once someone has actually walked it, on real
infrastructure, and recorded what happened — this document is the script,
not the recording. Each scenario has a "Drilled?" line to fill in.

---

## 1. Database corruption (data-level, not host loss)

- **Detection**: `LedgerReconciliationSweepMismatch`/
  `LedgerReconciliationMismatch` alerts firing, or Postgres itself
  reporting page-level corruption in its logs.
- **RPO**: Bounded by continuous WAL archiving — near-zero data loss up
  to the last archived segment, **if** archiving is confirmed live in
  production (see `DISASTER_RECOVERY.md`'s own "Is this actually
  running?" checklist — unconfirmed as of this document).
- **RTO**: Time to run `restore_pitr.sh` against the most recent
  basebackup plus WAL replay time to the target moment. No real
  measurement exists yet — depends on database size and basebackup
  recency, both unknown without a real backup schedule (LB-B2).
- **Commands**:
  ```bash
  ./deploy/restore_pitr.sh <basebackup-timestamp-dir> "<time just before corruption>"
  # Inspect the restored, throwaway container's data before promoting it.
  ```
- **Verification**: Row counts and a few known real transactions in the
  restored instance match what's expected; `packages/core/ledger.py::
  reconcile()` returns zero mismatches against the restored data.
- **Rollback decision**: Owner decides whether to promote the PITR
  restore to production (accepting the RPO gap) or attempt a narrower
  logical-level fix first (`INCIDENT_RESPONSE.md`'s ledger-imbalance
  playbook) if the corruption is scoped to something smaller than "the
  whole database."
- **Owner**: Whoever holds production DB access.
- **Drilled?**: Not yet — blocked on LB-B3 (no real restore drill against
  a production backup has been run).

## 2. Server loss (the whole production host disappears)

- **Detection**: Every `/healthz` endpoint unreachable, Cloudflare Tunnel
  shows the origin down.
- **RPO**: Same as scenario 1 (database-level), plus whatever isn't yet
  replicated off-host (a single-host deployment has no live standby — no
  evidence a second host/replica exists based on this repo's own
  `docker-compose.prod.yml`, which is a single-node compose file).
- **RTO**: Time to provision a replacement host + redeploy + restore. No
  real measurement exists; depends on infrastructure not codified as
  infrastructure-as-code in this repo (docker-compose alone doesn't
  provision a bare host).
- **Commands**:
  ```bash
  # On the replacement host:
  git clone <repo> && cd game && git checkout <last known-good SHA>
  cp .env.example .env  # then fill real production values
  docker compose -f deploy/docker-compose.prod.yml up -d
  ./deploy/restore.sh <latest-dump> jobingo   # or restore_pitr.sh for point-in-time
  ```
- **Verification**: All `/healthz` endpoints green, a real smoke test
  (deposit + play a round) succeeds.
- **Rollback decision**: N/A — this scenario *is* the recovery; there's
  no "roll back further" beyond restoring the most recent good backup.
- **Owner**: Whoever provisions production infrastructure.
- **Drilled?**: Not yet — requires a disposable second host, out of
  scope for this sandbox.

## 3. Redis loss (data loss, not just downtime)

- **Detection**: Every service's `/healthz` may still report alive
  (Redis isn't checked deeply by today's health endpoints — see
  `PRODUCTION_READINESS.md`'s Health endpoints row), but WS
  connections, rate limiting, notification delivery, and payout-stream
  processing all stop functioning correctly.
- **What's actually at risk**: Redis in this codebase holds *operational*
  state (room locks, rate-limit buckets, WS pub/sub, the `payouts` and
  `bot_notifications` Streams) — **not** the ledger or any durable
  financial record, which lives exclusively in Postgres. A Redis data
  loss cannot corrupt money; it can lose in-flight coordination.
- **Concrete blast radius of a Redis flush**:
  - Room locks: self-healing — `RoomLock.acquire()` re-acquires on the
    next attempt; at worst, two engine instances briefly race for a room
    (the Lua-scripted `SET NX` still prevents a real double-claim).
  - Payout stream (`PAYOUT_STREAM`): any payout enqueued but not yet
    consumed is lost from the stream, but the underlying `payments` row
    stays in Postgres — `sweep_stuck_approved_payouts()`
    (`services/payments/withdrawals.py`) is exactly the periodic sweep
    that re-enqueues anything stuck, closing this gap without manual
    intervention.
  - Notification deliveries: same shape — a delivery marked `processing`
    with no corresponding stream entry self-heals via
    `_reclaim_stuck_deliveries()` (15-minute threshold).
  - Rate-limit buckets: reset to empty — briefly more permissive, not a
    security hole (fails closed on a Redis *error*, per
    `PRODUCTION_READINESS.md`; a flushed-but-reachable Redis is not the
    same as a Redis error, so this is a real, if minor, gap worth noting:
    a flush is not an outage from the rate limiter's point of view).
- **RPO/RTO**: RPO is effectively N/A for financial data (nothing
  financial is only in Redis); RTO is however long it takes to restart
  the Redis container — seconds, since it holds no volume this codebase
  depends on for durability.
- **Commands**: `docker compose -f deploy/docker-compose.prod.yml restart redis`
- **Verification**: `redis-cli ping`, then confirm the periodic sweeps
  (bonus, withdrawal, ledger reconciliation, both provider
  reconciliations) pick back up within their own interval on the next
  tick — check `payments` container logs for the next scheduled run.
- **Rollback decision**: None needed — this is a stateless-recovery
  scenario by design.
- **Owner**: Whoever operates the production Redis container.
- **Drilled?**: Partially — the local dev Redis container was restarted
  mid-session this pass for an unrelated reason (see
  `PRODUCTION_READINESS.md` fix history) and the platform's sweeps
  self-healed as described; not drilled against production specifically.

## 4. Application restart (a normal deploy or crash-restart)

- **Detection**: N/A — this is routine, not an incident, as long as it
  completes cleanly.
- **RPO**: Zero — no data loss by design. `EngineWorker.shutdown()` calls
  each owned room's `RoundEngine.stop()` (a cooperative flag, not a raw
  `task.cancel()`) and waits for every task to exit on its own before the
  process exits, giving each round's own crash-recovery invariants (an
  in-flight round's state is fully persisted in Postgres, not held only
  in memory) a clean exit path rather than an abrupt kill.
- **RTO**: Time for `docker compose up -d --force-recreate` to pull/start
  the new image — typically seconds, not measured formally in production.
- **Commands**: `docker compose -f deploy/docker-compose.prod.yml up -d --force-recreate --no-deps <service>`
- **Verification**: `/healthz` green, `recover_orphaned_rounds()`
  (called by every `EngineWorker.start()`) logs recovering zero orphaned
  rounds if the prior shutdown was clean, or a real, bounded number if it
  wasn't (crash, not graceful stop) — either way, no round is left
  silently stuck.
- **Rollback decision**: Standard `docs/PRODUCTION_ROLLBACK.md` procedure
  if the new deploy itself is the problem.
- **Owner**: Whoever runs deploys.
- **Drilled?**: Implicitly, continuously — every test in
  `tests/integration/test_recovery.py` and `test_worker.py` exercises
  exactly this "engine restarts, orphaned rounds recover" path against
  real Postgres/Redis.

## 5. Payment-worker interruption

- **Detection**: `PayoutQueueDepthHigh` alert, or deposits/withdrawals
  visibly stalling while the rest of the platform (Bingo play) keeps
  working.
- **Blast radius**: `payout_worker.py` is one process running the payout
  stream consumer *and* now 6 periodic sweeps (deposit poll, withdrawal
  sweep, 2 provider reconciliations, bonus wagering, ledger
  reconciliation — see `LAUNCH_BLOCKERS.md`'s LB-A1) — a crash stops all
  7 at once. This single-point-of-failure shape is a known, accepted
  trade-off (`INCIDENT_RESPONSE.md` already documents it), not a new
  finding.
- **RPO**: Zero for anything already committed to Postgres — a crashed
  payout worker leaves `payments` rows in their last real status; nothing
  is lost, only paused. In-flight Redis stream entries not yet acked are
  redelivered via the consumer group's own `XAUTOCLAIM` on restart
  (`run_forever()`'s own crash-recovery, already built and tested).
- **RTO**: Container restart time (seconds) plus however long the next
  scheduled sweep interval is for anything that missed a tick while down
  (worst case: the 3600s/hourly reconciliation sweeps).
- **Commands**: `docker compose -f deploy/docker-compose.prod.yml up -d --force-recreate --no-deps payments`
- **Verification**: `payout_queue_depth` gauge returns to baseline;
  `payments_periodic_sweep_failed` stops appearing in logs.
- **Rollback decision**: If the crash was caused by a bad deploy, standard
  rollback; if by a provider-side outage (Chapa down), no rollback helps
  — use the `payment_provider_availability` flag instead
  (`INCIDENT_RESPONSE.md`'s playbook).
- **Owner**: Whoever operates the `payments` container.
- **Drilled?**: Covered by `tests/integration/test_payout_worker.py`'s
  crash-recovery tests against real Redis; not drilled against a live
  production instance.

## 6. Bingo-worker (engine) interruption

- **Detection**: `RoundVoided` alert rate spike, or players reporting
  rooms stuck/unresponsive.
- **RPO**: Zero — every round's state (entries, called numbers, pot) is
  persisted in Postgres as it happens, not held only in the engine
  process's memory; `recover_orphaned_rounds()` is exactly the mechanism
  that finds and correctly resolves (refund, not silently drop) any round
  whose owning engine died mid-flight.
- **RTO**: Container restart time (seconds) plus `run_active_rooms()`'s
  next poll cycle to re-claim every active room.
- **Commands**: `docker compose -f deploy/docker-compose.prod.yml up -d --force-recreate --no-deps engine`
- **Verification**: `recover_orphaned_rounds()`'s log line shows the
  actual number of rounds recovered (0 for a clean restart, a real bounded
  number for a crash); every previously-active room reappears in the
  lobby list within one poll interval.
- **Rollback decision**: Standard rollback if a bad deploy caused the
  crash-loop.
- **Owner**: Whoever operates the `engine` container.
- **Drilled?**: Yes, extensively, against real Postgres/Redis —
  `tests/integration/test_recovery.py` and this pass's own
  `test_worker.py` fix (see `PRODUCTION_READINESS.md`) both exercise real
  engine-restart-and-recover scenarios.

## 7. Cloudflare Tunnel interruption

- **Detection**: All 5 production hostnames unreachable from outside,
  while the origin's own `/healthz` (checked directly on the host,
  bypassing the tunnel) is still green — this specific combination is
  what distinguishes a tunnel outage from an actual application outage.
- **RPO**: N/A — no data path runs through the tunnel that isn't also
  durable at the origin (the tunnel is pure network transport).
- **RTO**: Depends entirely on Cloudflare's own tunnel-recovery behavior
  and whatever supervises `cloudflared` on the host (`systemctl restart
  cloudflared` if it's a systemd service — unconfirmed from this
  environment which supervisor, if any, is configured in production).
- **Commands**:
  ```bash
  systemctl status cloudflared      # or the container-equivalent, if run as one
  cloudflared tunnel info <tunnel-name>
  systemctl restart cloudflared     # if status shows it's down
  ```
- **Verification**: `curl -sI https://arada.fun/healthz` succeeds again
  from outside the host's own network.
- **Rollback decision**: N/A — a pure connectivity restoration, not a
  code/data decision.
- **Owner**: Whoever holds the Cloudflare account and host-level access
  to `cloudflared`.
- **Drilled?**: Not yet — requires production host access this
  environment doesn't have (`LAUNCH_BLOCKERS.md` LB-B4/LB-B6 territory).

---

## Summary: which of these are launch blockers vs. informational

| # | Scenario | Real gap? | Tracked as |
|---|---|---|---|
| 1 | DB corruption | RPO/RTO depend on an unwired backup schedule | LB-B2, LB-B3 |
| 2 | Server loss | No IaC/replica; RTO unmeasured | Not separately tracked — accepted single-host risk at current scale |
| 3 | Redis loss | Self-healing by design; rate-limit-reset is a minor, non-blocking gap | Informational only |
| 4 | App restart | Already correct and tested | None |
| 5 | Payment-worker interruption | Single-point-of-failure is a known, accepted trade-off | Informational only |
| 6 | Engine interruption | Already correct and tested | None |
| 7 | Cloudflare Tunnel interruption | Requires production access to drill | LB-B4/LB-B6 |

No RPO/RTO number in this document was invented — every one is either
derived from a real, tested mechanism (WAL archiving, cooperative
shutdown, Postgres-durable round state) or explicitly marked as depending
on a real schedule/infrastructure decision that doesn't exist yet.
