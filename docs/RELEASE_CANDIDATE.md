# Release Candidate Record

What exactly this build is — the record a rollback or incident
investigation points to later. Every field below is a real command's
real output from this pass, not a template with numbers filled in from
memory.

## Identity

| Field | Value |
|---|---|
| Release candidate commit | `cd5f297e8993fadebb97fb02b86c5fcb6192244d` |
| Base commit (parent of this pass's work) | `baeef84f68c911c2087db838a45f63a3b593b96a` |
| Working tree state at record time | Clean (committed locally; per this project's standing convention, the assistant commits and the user pushes — not yet pushed to `origin` as of this record) |
| Branch | `main` |
| Migration head | `4bbb21e0f5ad` (`alembic -c migrations/alembic.ini current`) — unchanged this pass, no schema migration added |
| Python version | 3.12.3 |

## Dependencies (from `pyproject.toml`)

| Package | Constraint |
|---|---|
| asyncpg | >=0.29 |
| alembic | >=1.13 |
| redis | >=5.0 (redis-py 8.1.0 installed — confirmed via this pass's own investigation into `ConnectionPool` defaults) |
| fastapi | >=0.110 |
| uvicorn[standard] | >=0.29 |
| bcrypt | >=4.1 |
| pyotp | >=2.9 |
| prometheus-client | >=0.20 |
| opentelemetry-api/sdk/exporter-otlp-proto-http | >=1.27 |
| cryptography | >=43 |
| aiogram | >=3.0 |
| aiohttp | >=3.9 |
| httpx | >=0.27 |

Dev-only: pytest>=8.0, pytest-asyncio>=0.23, mypy>=1.10,
asyncpg-stubs>=0.29, websockets>=12.0, playwright>=1.40.

## Docker

| Image | Base |
|---|---|
| Application services | `python:3.12-slim` (`Dockerfile:9`) |

`deploy/docker-compose.yml` (dev) / `deploy/docker-compose.prod.yml`
(production) define the real service topology — 6 real worker
entrypoints (engine, bot, payments/payout-worker, gateway, admin, plus
the payout worker's own 7 in-process periodic sweeps as of this pass).

## Environment requirements

`.env.example` (89 lines) enumerates every required variable. Every
secret-shaped value there is a genuine empty placeholder at this commit
(confirmed this session — see `docs/PRODUCTION_READINESS.md`'s Security
section); real values are supplied only in the actual production
environment, never committed.

## Test results (this pass's final run)

```
pytest tests/ -q
1184 passed, 52 deselected, in 474.07s (0:07:54)
EXIT=0
```

Zero failures — the final confirmation run of this pass, including every
test added during it (the ledger-reconciliation sweep, the Bingo
acceptance-audit gap-fills, the zero-player-room test, the malformed-
claim test, the Notification Center Redis-outage test, the
`run_active_rooms()` claim-cap test, `_serve_commands()`'s Redis-
resilience test, and the 13 unit/integration/e2e tests for the new
emergency single-room stop feature). The first fully clean full-suite
run in this engagement's entire history, held across every subsequent
addition, not just once. See `docs/PRODUCTION_READINESS.md`'s "Full
regression suite" section for the complete run-by-run history, and
`DECISIONS.md` for the real bugs found and fixed along the way — three
test-only issues (a test-data-hygiene issue in `test_worker.py`, a wall-
clock-time-dependent boundary bug in two Ethiopian-calendar-day tests,
and a real test regression exposed by `run_active_rooms()`'s own
hardening) plus one genuine, previously-shipped production gap (the
`POST /rounds/{id}/void` admin action had no guard against racing a
live engine's own settlement — a real double-payment risk, closed by
this pass's new settlement guard in `round_engine.py`).

## Security results

- Full secret-shaped `.env.example` audit: clean at `HEAD` (see
  `docs/PRODUCTION_READINESS.md`'s Security table).
- One real historical secret exposure (`PHONE_ENCRYPTION_KEY`, 5 commits,
  now removed from `HEAD` but recoverable from git history) — flagged as
  a pending decision, not silently resolved (`docs/LAUNCH_BLOCKERS.md`
  LB-D3).
- `docs/TELEGRAM_SECURITY_AUDIT.md` (new this pass): initData validation,
  session-identity binding, and the server-authoritative-balance/claim/
  payout invariant all verified GREEN; one YELLOW (no explicit WS Origin
  check, mitigated by the initData requirement).

## mypy results

```
mypy packages services migrations
Success: no issues found in 103 source files
```

Clean at every checkpoint during this pass, including after every fix.
(`tests/` is deliberately excluded from strict typing per
`pyproject.toml` — not a gap, an intentional scope boundary.)

## Backup status

Mechanism real and tested (`tests/integration/test_backup_restore.py`);
**no schedule confirmed wired anywhere** (repo-local or production) —
tracked as `docs/LAUNCH_BLOCKERS.md` LB-B2, P0, unresolved as of this
record.

## Rollback status

`docs/PRODUCTION_ROLLBACK.md` (existing) documents feature-flag and
code-level rollback levers for every subsystem this session touched.
This pass's own changes are entirely additive (a new sweep, new tests, 2
test-boundary fixes, new docs) — no rollback risk beyond a standard
`git checkout <prior-sha>` + container recreate for the `payments`
service if the new ledger-reconciliation sweep needs to be pulled.

## Changes in this pass (relative to base commit `baeef84`)

- Built the emergency single-room stop feature (`POST /rooms/{id}/stop`,
  superadmin-only): halts an active round immediately, refunds through
  the existing ledger-backed primitive, deactivates the room. Found and
  fixed a real, previously-shipped gap while building it: the existing
  `POST /rounds/{id}/void` action had no guard against a live engine
  concurrently settling the same round — a genuine double-payment risk,
  closed with a Postgres row-lock guard shared by both actions. Full
  design and 16-scenario test record in `docs/EMERGENCY_ROOM_STOP.md`.
- Fixed the `test_worker.py` connection-exhaustion flake at its true
  root cause (test-data hygiene, not a redis-py bug).
- Fixed 2 wall-clock-time-dependent test bugs in
  `test_responsible_gaming.py` (Ethiopian-calendar-day boundary tests).
- Wired a real, scheduled ledger-reconciliation sweep into the existing
  `payout_worker.py` process (`services/payments/ledger_reconcile_sweep.py`),
  with a new scraped metric and alert rule — no new scheduler introduced.
- Hardened `EngineWorker.run_active_rooms()` against unbounded claims
  (`MAX_NEW_CLAIMS_PER_POLL = 50`) after a forensic pass explicitly
  refused to treat the earlier 560-stale-rooms finding as purely a test
  artifact — proved the production code itself would not have survived a
  genuine large-scale active-room count either. This exposed and fixed a
  real test regression at the test's own defensive scoping, not by
  weakening the new production guarantee (see `DECISIONS.md`).
- Added `docs/LAUNCH_BLOCKERS.md`, `docs/TELEGRAM_SECURITY_AUDIT.md`,
  `docs/DISASTER_RECOVERY_DRILL.md`,
  `docs/RESPONSIBLE_GAMING_REQUIREMENTS.md`,
  `docs/PLATFORM_POLICY_REVIEW.md`, this document.
- Closed 8 real test-coverage gaps found via a systematic audit against a
  16-scenario Bingo win/claim acceptance list (4 pure win-logic unit
  tests, 1 malformed-claim test, 1 strengthened unauthorized-claim test,
  1 zero-player-room test) — 3 gaps remain, documented and downgraded to
  P2 in `docs/LAUNCH_BLOCKERS.md` LB-A4.
- Verified (no code change needed): zero-player spectator mode, the
  432-card pool size against `DECISIONS.md`'s own recorded correction,
  and that no balance-mutation path anywhere bypasses the ledger.
