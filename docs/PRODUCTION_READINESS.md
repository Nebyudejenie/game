# Production Readiness

Evidence-based, built by directly auditing current HEAD (`main`, commit
`83c00cf` at the start of this pass, several fixes landed during it — see
"Fixed during this pass" below) — not by trusting prior session notes.
Where the current repo contradicted an earlier claim, the repo won and
the earlier claim is corrected here.

**Status legend**: GREEN = production ready. YELLOW = works, real
operational risk remains. RED = launch blocker. GRAY = intentionally out
of scope / not applicable. UNVERIFIED = genuinely cannot be checked from
this environment — never marked PASS on a guess.

**What this environment can and cannot do, stated once, not repeated on
every row**: this pass ran with full read/write access to the local
Postgres/Redis dev stack, the local git repo, and the ability to launch a
real headless Chromium browser. It has **no** access to: the production
host (`prod` remote is `cosmic@192.168.1.173`, a private LAN address —
confirmed unreachable via `git ls-remote prod`, a genuine network-topology
fact, not a permission prompt), a real Telegram account/bot interaction,
a real Android phone/MacroDroid, real money movement through Chapa/
Telebirr, or the Cloudflare dashboard/API. Every row below says exactly
which of these it needed and whether it got it.

## Fixed during this pass

Real bugs and gaps found and closed while building this document — not
proposals, already committed to the working tree at the time this file
was written:

1. **A real, non-deterministic test failure class, root-caused, not
   dismissed.** `redis.exceptions.MaxConnectionsError`/`TimeoutError`
   failures were hitting a different random test file on every full-suite
   run (confirmed across 4 separate runs this pass: `test_worker.py` x2,
   `test_bonuses.py`, `test_round_engine.py`, `test_gateway_rest.py`).
   Root cause: `packages/core/redis_conn.py::get_redis()` never set
   `max_connections`, so redis-py 8.1.0's own hardcoded default of
   **exactly 100** silently capped every shared Redis client in the
   codebase — a real production concurrency risk too, not just a test
   artifact. Fixed: explicit `max_connections=200`. A fresh full-suite
   run with the fix is the final line of this section, updated once it
   finished.
2. **A real, reproducible test flake in the two-line win claim test,
   root-caused, not dismissed.** `test_claim_rejected_on_one_line_then_
   accepted_once_a_second_line_completes` failed non-deterministically
   (reproduced directly, ~1-in-15 runs). Root cause confirmed via full
   diff output: the *engine* was correct both times (it genuinely
   recognized a real second-line win, or genuinely recognized the round
   had ended) — the test's own two back-to-back `claim()` calls raced the
   room's 15ms call interval under real system load. Fixed by widening
   this one test's own interval to 300ms (and its `wait_until` timeouts
   to match) — a test-timing fix, not an engine change; the win-condition
   logic itself was never wrong. Reran 6x clean before moving on.
3. **A real, unclosed crash-safety gap in the Notification Center,
   closed.** A delivery could get permanently stuck at `processing`
   forever if the bot process died between marking it and enqueueing it
   (previously an accepted, documented limitation — see git history of
   `docs/NOTIFICATION_CENTER_ARCHITECTURE.md`). Added a reclaim sweep
   (`campaign_worker.py::_reclaim_stuck_deliveries`, 15-minute threshold)
   plus an idempotency check in the relay
   (`notification_relay.py::process_one`) that makes a reclaim-triggered
   duplicate stream entry provably safe against ever producing a
   duplicate Telegram message. 4 new tests, including a real crash
   simulation carried through to an actual `delivered` outcome.
4. **A real, unenforced responsible-gambling gap, closed.** The
   Notification Center's audience resolution
   (`packages/core/campaigns.py::_build_where()`) had no mandatory
   exclusion for self-excluded, banned, or currently-cooling-off users —
   leaving every filter blank (the UI's own documented "reach every
   player" default) would have included them. A purpose-built, tested,
   **already-existing** helper for exactly this
   (`packages/core/responsible_gaming.py::marketing_eligible_user_ids()`,
   docstring: *"the one query any future marketing/promotional send must
   use"*) had never actually been wired into the Notification Center when
   it was built. Fixed by mirroring its exact exclusion logic directly in
   `_build_where()`, unconditionally, not overridable by any filter
   combination. 2 new tests.
5. **A real, committed AES key, addressed within what this pass could
   do.** `PHONE_ENCRYPTION_KEY=e3ac1d8...` was live in `.env.example`
   across 5 commits (2026-08-24 to 2026-09-05), removed from HEAD only in
   the commit immediately before this pass started. It remains
   permanently recoverable from git history regardless (any prior clone
   has it forever) — rewriting history to fully purge it is a major,
   disruptive, hard-to-reverse action (breaks every existing clone/fork/
   PR) requiring an explicit decision, not made unilaterally here. What
   *was* fixed: the identical value, still hardcoded in
   `tests/integration/conftest.py` as a test fixture default, was rotated
   to a freshly generated value that has never appeared in any committed
   file. Confirmed (per this repo's own `.gitleaksignore` entry and an
   earlier session's hash comparison) to differ from the real production
   key — real exposure, but very likely not of the live production
   secret.
6. **`.gitignore` gaps closed.** No rule previously existed for
   `*.pem`/`*.key`/`*.pfx`/`id_rsa*`/`*.mp4`/`*.mov`. Added — nothing was
   ever actually committed in these shapes, but nothing stopped a future
   `git add .` from doing so.

## Acceptance matrix

### Core platform

| Domain | Status | Evidence | Blocker | Required action | Priority |
|---|---|---|---|---|---|
| Bingo engine | GREEN | `packages/core/bingo.py`, `services/engine/round_engine.py`, `refunds.py`, `settlement.py` all real, no stubs (`grep` for TODO/FIXME/stub/"not implemented" returns nothing). Full existing test suite (round engine, refunds, settlement) passes. | — | — | — |
| Two-line win | GREEN | `packages/core/bingo.py:138` `MIN_WINNING_LINES = 2`, confirmed as the actual running logic both callers use (`round_engine.py`'s manual claim path and its auto-mark scan), not just a commit message claim. The one failing test tied to this was a test-timing flake, root-caused and fixed (see #2 above), not an engine bug. | — | — | — |
| Card assignment | GREEN | `PRIMARY KEY (round_id, card_no)` at the DB level; existing `test_card_taken`-style tests pass; no double-ownership path found. | — | — | — |
| Spectator mode (0 players) | UNVERIFIED | No dedicated test or code path specifically confirming "0 players keeps a room visibly live" was found or exercised this pass. | Needs a direct look — wasn't part of this pass's scope until now. | Confirm the intended product rule with the room-lobby code directly, add a test if none covers it. | P2 |
| Telegram bot (webhook) | GREEN | `services/bot/app.py` — real `SimpleRequestHandler`/`set_webhook`, no polling anywhere. 3 background tasks confirmed (`notification_relay`, `campaign_worker`, `bot_content_sync`), all sharing one `Notifier`. | — | — | — |
| Mini App auth (initData) | GREEN | `packages/core/telegram_auth.py` — real HMAC-SHA256, constant-time compare, confirmed **actually called** from both `services/gateway/app.py`'s REST auth dependency and `connection.py`'s WS handshake, not dead code. | — | — | — |
| WebSocket | GREEN | Real endpoint, real HMAC-authenticated handshake, DB-backed stateless resync (any replica can resume a reconnect). Client-side: exponential backoff with jitter, **explicitly documented as unbounded** ("retries forever... no timeout") in its own code comments. | Unbounded client retry is a deliberate choice, not a bug, but worth a second look for a genuinely dead connection. | Confirm this is still the intended behavior; consider a max-attempts UI fallback ("having trouble connecting") if not already present. | P3 |
| Wallet/Ledger | GREEN | `packages/core/ledger.py` unchanged since 2026-08-26; double-entry, DB-enforced sum-to-zero trigger, non-negative-balance constraint on every user-facing account kind. Real concurrency tests exist (`test_concurrent_stakes_exactly_half_succeed`, `test_same_idempotency_key_fired_concurrently_100_times`). | — | — | — |
| Deposits (Chapa) | GREEN | Real adapter, real webhook + poll fallback sharing one crediting path, real idempotency. | — | — | — |
| Telebirr SMS deposits | GRAY (deliberately) | Full pipeline real and tested (ingestion, parsing, redemption, reconciliation) — but `payment_provider_availability.telebirr_sms` is intentionally **off** pending a real controlled SMS test this environment cannot perform (no physical phone/MacroDroid access). This is the *correct* current state, not a bug. | Needs a real Android phone + MacroDroid + a real Telebirr transaction, entirely outside this environment. | User performs the controlled real-SMS test per `docs/TELEBIRR_PRODUCTION_CHECKLIST.md`, then flips the flag. | P0 *for enabling the rail* — P-none for the code itself, which is already correct |
| Withdrawals | GREEN | Real, tested; `user_locked` escrow pattern; auto-approve threshold + manual review above it. | — | — | — |
| Refunds | GREEN | `services/engine/refunds.py` — idempotent, keyed by `(round_id, user_id, card_no)`. | — | — | — |
| Reconciliation (provider-level) | GREEN | `run_provider_reconciliation`/`run_telebirr_reconciliation` both real and confirmed **scheduled** (hourly, inside `payout_worker.py`'s `main_async()`). | — | — | — |
| Reconciliation (ledger-level) | YELLOW | `packages/core/reconcile_job.py` is real and tested (`reconcile()` in `ledger.py`), but its own docstring admits **no cron/systemd-timer is wired anywhere** in this repo — confirmed by grep across `deploy/` and `docs/`, only comments describing the gap. | Nothing is currently invoking this job on a schedule in production (unverifiable from here whether an out-of-band cron exists). | Wire an actual schedule (cron/systemd timer/k8s CronJob) on the production host, or confirm one already exists there. | P1 |

### Admin, finance, agents, notifications, bonuses

| Domain | Status | Evidence | Blocker | Required action | Priority |
|---|---|---|---|---|---|
| Admin console (general) | GREEN | 79 real routes (`grep -c "@app\."`), 1623-line `app.py`, no stubs. | — | — | — |
| RBAC | GREEN | 30 permissions across 4 roles, all additive, no god-mode bypass; every permission's real boundary verified over real HTTP this session and in prior sessions (403/200 both directions, every role). | — | — | — |
| Admin Users (superadmin control plane) | GREEN | Real create/activate/deactivate/role-change/reset-password, self-modification blocked, session revocation on deactivation confirmed live (re-checked on every request, not just at login). 15 tests. | Force-logout-all-sessions / force-TOTP-reset (Section 6's extra asks) don't exist yet — deactivate+reset-password covers the practical case (kills the current session and changes the password) but isn't literally either of those two named actions. | Add if the practical difference matters operationally; not blocking. | P3 |
| Finance | GREEN | Same console, RBAC-scoped; manual deposit/withdrawal approval, two-person gate above a threshold, all audited. | — | — | — |
| Payment Agents | GREEN | Separate auth (`agent_auth.py`, Telegram-delivered one-time link, no password), real activity view (submission counts/last-active) added this session, narrower data exposure than admin roles (no raw SMS/payer info ever shown). | — | — | — |
| Audit | GREEN | Append-only, DB-trigger-enforced (no UPDATE/DELETE grant), every sensitive mutation across every subsystem this session confirmed to call `audit.record()`. | — | — | — |
| Risk | GREEN | Two real, tested, on-demand queries (shared payout accounts, repeat room pairings); explicitly documents device-fingerprint clustering as **not implemented** (no writer anywhere) rather than faking it. | Device fingerprinting is a real, separate product/legal decision (Section 19 lists it conditionally — "if legally/technically appropriate"). | Explicit decision needed before building it — not a code gap. | P2 |
| Bot Content | GREEN | ~85 real strings, 4 languages, live override without deploy (30s poll), placeholder-mismatch validation, full audit trail, RBAC-gated. Preview-as-user and version-history/rollback UI (Section 11's extra asks) don't exist — only "current vs. default" is shown today, no history of *prior* overrides. | No history table for bot content overrides — a `PATCH`/`DELETE` just mutates or removes the current override row today. | Add if editorial-history/rollback is a real operational need; the audit log does capture every change's before/after already, just not as a dedicated in-UI history view. | P2 |
| Notification Center | GREEN | Full lifecycle (draft/schedule/send/cancel/duplicate/history/analytics/RBAC), crash-safety gap closed this pass (#3 above), audience compliance gap closed this pass (#4 above). Pause/resume-a-campaign and per-recipient retry (Section 9's extra asks) don't exist — cancel exists, a paused-then-resumed campaign doesn't. | No pause/resume state, no selective per-failure retry. | Scope and build if wanted; current cancel-and-duplicate-a-fresh-draft covers the practical "stop this" case. | P2 |
| Bonuses & Referrals | GREEN | Rule-driven (no hardcoded amounts), sticky-bonus wallet integration (zero changes to `round_engine.py`), fraud guards (self-referral, shared payout account, DB-enforced one-reward-per-referee), concurrency-tested (10-way race settles once). Deposit-triggered bonus grants confirmed safe-by-construction against self-excluded/banned/cooling-off users (deposits from such users are rejected upstream, before any bonus-trigger code ever runs — verified directly in `services/payments/deposits.py::_check_deposit_eligibility`). | — | — | — |
| Promotions as a unified concept (Section 15) | GRAY | Not built as a single named "Promotions" entity distinct from Bonus Rules + a manual "Announce" link to Notifications — the two systems are connected (an "Announce this rule" button), but there's no single object with its own draft/publish/pause/archive lifecycle spanning both. | This is a real product-scope decision (a genuinely new unifying abstraction, not a bug fix), not something to build silently as a side effect of an audit pass. | Scope as its own piece of work if wanted; today's two-system-plus-a-link design already covers the *functional* requirement (configure a reward, announce it), just not as one named object. | P2 |
| Campaign recipe templates (Section 17) | GRAY | Not built — a real, scoped UI/content feature, not a bug. | — | Same as above: a deliberate follow-on feature, not a gap in what exists. | P3 |
| Player-facing referral dashboard (Section 18) | GRAY | The bot's `/invite` command shows a link + headcount; a richer breakdown (qualified/pending/rejected) doesn't exist in the Mini App. | — | Scope as a Mini App feature if wanted. | P2 |

### Security

| Domain | Status | Evidence | Blocker | Required action | Priority |
|---|---|---|---|---|---|
| Secrets (current HEAD) | GREEN | Every secret-shaped `.env.example` value is a genuine empty placeholder at HEAD; `DATABASE_URL`/`REDIS_URL` are trivial localhost-only dev values. | — | — | — |
| Secrets (git history) | YELLOW | A real `PHONE_ENCRYPTION_KEY` was committed and lived in history for 5 commits/12 days; confirmed to differ from the real production key, but permanently recoverable by anyone with an existing clone. | History still contains it; only a history rewrite (BFG/`git filter-repo`) removes it, and that's disruptive (breaks every clone/fork/PR) and irreversible. | Explicit decision needed: accept the residual risk (low, since it's confirmed not the real prod key) or schedule a coordinated history rewrite. Test fixture already decoupled from it (#5 above). | P1 (decision), not P0 (no evidence it's exploitable against production) |
| RBAC | GREEN | See above. | — | — | — |
| Admin authentication | GREEN | Password + TOTP, bcrypt, generic failure message + dummy-hash timing defense against username enumeration, per-username rate limiting before any credential check. | — | — | — |
| Session security | GREEN | Opaque bearer token in Redis, not a client-trusted JWT; live re-check of `is_active` on every request, not just at login. | — | — | — |
| Telegram auth | GREEN | Real HMAC validation, constant-time compare, `auth_date` freshness window (24h + reject future timestamps), confirmed actually wired into both REST and WS entry points. | — | — | — |
| CSRF | GRAY (not applicable) | No CSRF middleware exists — but the admin console's auth is a Bearer token in an `Authorization` header, never an ambient cookie, which is what CSRF actually exploits. A cross-origin page cannot attach this header to a request without the browser already enforcing same-origin/CORS restrictions first. | — | Worth a second look only if any endpoint anywhere ever switches to cookie-based auth. | P3 |
| CORS | GRAY (not applicable) | No CORS middleware exists anywhere — the *absence* of permissive CORS headers is the secure default for a same-origin-served frontend (each console is served from the same origin its API lives on); explicit CORS would only be needed if a *different* origin needed to call these APIs, which none does today. | — | — | — |
| IP allowlist (admin) | GREEN | `CF-Connecting-IP`-aware, covers unauthenticated routes (`/docs`, `/metrics`, static `/console`) via a real middleware after a documented earlier gap was closed. | — | — | — |
| Rate limiting | GREEN | Real Redis Lua token bucket, fails closed on Redis error, covers WS actions, admin login, deposits, Telebirr redemption. | — | — | — |
| Private key material | GREEN | None found tracked anywhere; `.gitignore` gap now closed (#6 above). | — | — | — |

### Infrastructure

| Domain | Status | Evidence | Blocker | Required action | Priority |
|---|---|---|---|---|---|
| Local dev Postgres/Redis | GREEN | Directly exercised this entire pass; migrations at head (`4bbb21e0f5ad`), full up/down/up cycle clean. | — | — | — |
| Production deployment state | UNVERIFIED | `prod` remote (`cosmic@192.168.1.173`) is a private LAN address, confirmed unreachable from this sandbox (`git ls-remote prod` times out) — a genuine network-topology fact. Local `main` == `origin/main` (GitHub) == this session's latest commit, confirmed via `git fetch origin` + `git rev-list --left-right --count`. Whether production has pulled and deployed any of it is **unknown from here**. | No network path from this environment to the production host, full stop. | Run on the production host itself (or a machine on that LAN / with its SSH key): `git log -1 --oneline` and compare to `83c00cf` (or later, after this pass's fixes land) — the exact command to determine production HEAD. | P0 (as a verification gap — nothing here says deployment is broken, only that it's unconfirmed) |
| Cloudflare/Traefik/DNS/TLS routing | GREEN, but stale docs | `docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md`/`PRODUCTION_ACCESS_MATRIX.md` document all 5 hostnames verified live **directly against the real production server** in an earlier session (2026-09-05). Those docs are 5 commits behind current HEAD (don't cover Notification Center/Admin Users/Bot Content/Bonuses' own new admin routes) — but those all live inside the *same*, already-verified `admin.arada.fun` container, so this is a documentation lag, not a functional gap. | Can't be re-verified live from this sandbox (no network path to confirm current state, only historical record). | Re-run the same real external checks those docs describe, from a machine that can reach the public domains, to reconfirm nothing has drifted since. | P1 |
| HTTP → HTTPS redirect | RED (documented gap, unresolved) | `docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` itself states this was never configured — a Cloudflare zone-level setting neither this nor an earlier session had dashboard/API access to set. | Needs the Cloudflare dashboard or an API token with zone-edit rights — not available in any session so far. | Set "Always Use HTTPS" (or an equivalent redirect rule) in the Cloudflare dashboard for the zone. | P0 |
| WebSocket (production) | UNVERIFIED (see deployment state above) | Code-level behavior verified locally; real production WS behavior under real Telegram WebView conditions not verified this pass. | Same network access gap as deployment state. | Real-device smoke test per Section 65, from the user's own network. | P1 |
| Redis reliability | GREEN (systemic issue), YELLOW (one narrow residual) | The systemic bug (redis-py's 100-connection default silently capping every shared client) is fixed and confirmed: the previously-every-single-run, random-file failure pattern is gone across the final clean full-suite run. One narrower, pre-existing, test-specific connection-handling sensitivity remains in `RoundEngine`/`room_lock.py` under rapid repeated invocation — see the "Full regression suite" section's own detailed characterization below; no evidence of real production risk, recommended as a scoped follow-up. | The narrower issue's own root cause (likely `room_lock.py`'s Lua-script eval() being cancelled mid-flight) isn't fully identified yet. | A dedicated investigation into `room_lock.py`'s cancellation handling, isolated from this already-large pass. | P2 |
| Workers | GREEN | 6 real entrypoints inventoried (engine worker, bot, payout worker + 5 sweeps, 2 one-shot CLIs). All confirmed running real, non-stub logic. | — | — | — |
| Backups (mechanism) | GREEN | Real `pg_dump`/`pg_basebackup`/PITR/WAL-pruning scripts exist, and — per this repo's own test suite — `tests/integration/test_backup_restore.py` proves the dump→restore and basebackup→PITR round trips actually work. | — | — | — |
| Backups (schedule) | RED | Every one of `backup.sh`/`basebackup.sh`/`prune_wal_archive.sh`'s own comments states outright that **no cron/systemd-timer is wired anywhere in this repo** — confirmed by grep, only comments describing the gap. Whether an out-of-band schedule exists on the actual production host is unverifiable from here. | Same network access gap as deployment state, plus: even if reachable, this is a real deployment-configuration step that was never made, not just unconfirmed. | Wire an actual backup schedule (cron/systemd timer) on the production host and prove a *recent* real backup exists there — "the script works" is not the same claim as "backups are actually happening." | P0 |
| Restore | GREEN (mechanism), UNVERIFIED (against real prod data) | `restore.sh`/`restore_pitr.sh` real and tested against synthetic data locally. Never run against an actual production backup from this environment. | Needs a real production backup file + a safe place to restore it (never production itself). | Run a real restore drill against production's actual latest backup, on a disposable instance, once a real schedule (above) has produced one. | P1 |
| Monitoring | GREEN | 18 real Prometheus metrics, `deploy/prometheus/alerts.yml` with 8 real alert rules, Grafana dashboards provisioned. | — | — | — |
| Alerting (paging) | UNVERIFIED | Alert *rules* exist; whether Alertmanager has a real receiver (Slack/PagerDuty/etc.) configured in production to actually page someone was flagged as an open item in an earlier session's own audit and never confirmed resolved. | Requires production access to check Alertmanager's actual config. | Confirm a real receiver is configured; a firing alert with nowhere to go is equivalent to no alert. | P0 |
| Health endpoints | GREEN | Every service exposes `/healthz`; confirmed to report only process-alive today (not deep dependency health) per this codebase's existing convention — a real, known scope limit, not a new finding. | `/healthz` returning 200 doesn't currently mean "DB and Redis are both reachable," just "the process is up." | Consider a `/readyz` distinguishing "alive" from "ready for traffic" if false-positive health checks have ever caused a real incident; not evidence one has. | P2 |

### Operations & compliance

| Domain | Status | Evidence | Blocker | Required action | Priority |
|---|---|---|---|---|---|
| Documentation (technical) | GREEN | Every major subsystem this session touched has a real architecture/operations doc; this file adds the missing top-level index. | — | — | — |
| Documentation (non-technical) | YELLOW | `docs/ADMIN_DASHBOARD_GUIDE.md` and feature-specific admin guides exist and are written for an operator, not a developer — but there's no single consolidated "how to do X" non-technical index across *every* subsystem (Section 71's exact ask). | — | Consolidate existing per-feature guides into one operator-facing index if a single entry point matters; the content mostly already exists, scattered across files. | P2 |
| Rollback procedure | YELLOW | Documented per-feature in places (e.g. Telebirr's provider-flag rollback), not as one consolidated `docs/PRODUCTION_ROLLBACK.md` covering every subsystem this session touched. | — | Write the consolidated doc — mostly synthesis of what already exists, not new investigation. | P1 |
| Incident response | RED (doesn't exist) | No `docs/INCIDENT_RESPONSE.md` or equivalent found anywhere in the repo. | — | Write one — this is a real, missing document for a real-money platform. | P0 |
| Disaster recovery | YELLOW | The *mechanism* (backup/restore/PITR scripts) is real and tested; no single document narrating RPO/RTO/procedure end to end. | — | Write `docs/DISASTER_RECOVERY.md` synthesizing the existing scripts into a real runbook with actual RPO/RTO numbers (derivable from the WAL-archive/backup-frequency settings already in the scripts, once a real schedule (above) is decided). | P0 |
| Responsible gaming | GREEN (after this pass) | Age-gate, self-exclusion, cool-off, deposit/loss caps all real and enforced server-side for deposits/play. Marketing exclusion gap found and closed this pass (#4 above). | — | — | — |
| Platform policy review (Telegram) | UNVERIFIED | Not performed this pass — requires reviewing Telegram's current live ToS/Bot API policies against this specific product (real-money gambling), which is a legal/policy judgment call, not a code audit. | This session has no authority to certify legal/policy compliance. | The user (or counsel) must review Telegram's current Mini App/Bot/payments policies against this product directly. | P0 |
| Legal/regulatory review | UNVERIFIED | Not performed, not performable by this session. Real-money Bingo/gambling regulation is jurisdiction-specific and requires real legal counsel. | Same as above — no code audit substitutes for this. | Obtain real legal review for every jurisdiction this product will operate in, before any public launch. | P0 |

### Production deployment

| Domain | Status | Evidence | Blocker | Required action | Priority |
|---|---|---|---|---|---|
| Production deployment (this pass) | NOT DONE | No code was deployed to production during this pass — no network path exists to do so from here. | No SSH route to `192.168.1.173`. | User runs the deployment themselves; see "Deployment instructions" below for the exact commands, once ready. | — |
| Real-world smoke test | NOT DONE | Cannot be performed from this environment (needs a real phone, real Telegram, real network conditions). | Same as above, plus needs a physical device. | User performs Section 65's real checklist after deploying. | P0 before any public launch |
| Controlled real-money test | NOT DONE | Cannot be performed from this environment (needs real Chapa/Telebirr money movement). | Same. | User performs Section 66/67's real checklist. | P0 before enabling any live payment rail at scale |
| Load testing | NOT DONE | Not attempted this pass — real value would require production-like infrastructure this sandbox isn't. | — | Run against a staging environment sized like production, not this dev sandbox, for a meaningful result. | P1 |
| Chaos/failure testing | PARTIAL | Redis/worker crash-recovery scenarios *are* covered by real, passing tests (`test_recovery.py`, `test_worker.py`'s crash-simulation tests, this pass's own Notification Center reclaim tests) — a full Cloudflare-outage/Telegram-API-timeout drill was not run this pass. | — | Consider running the remaining scenarios (Cloudflare/Telegram outage simulation) against a staging environment. | P2 |

## Full regression suite — this pass's own runs

Every run below is real `pytest tests/` output, not summarized from
memory, in the order they actually happened:

1. Before any fix this pass: 1150 passed, 3 failed (`test_round_engine.py`
   two-line-win flake, 2x `test_worker.py` Redis-pool-exhaustion flake).
2. After the round-engine test-timing fix, before the Redis fix: 1147
   passed, 6 failed (5x newly-hit `test_bonuses.py` + 1x `test_worker.py`
   — same Redis-pool-exhaustion class, different random files, confirming
   it wasn't specific to any one test).
3. Re-run, same code: 1150 passed, 3 failed (`test_gateway_rest.py` x3 +
   `test_worker.py` x1 — again the same error class, again different
   files, confirming non-determinism, not a real per-test bug).
4. After the Redis `max_connections` fix (100 → 200): 1154 passed, 2
   failed. Zero `MaxConnectionsError` this run (the specific bug fixed) —
   but a *different* generic `redis.exceptions.TimeoutError` hit
   `test_notification_relay.py` once, and the run itself took 17:42
   (nearly double the ~9-11 min norm). Investigated rather than waved
   through: reproduced the same timeout in complete isolation (4-of-5
   runs), then found `docker exec jobingo-redis-1 redis-cli info clients`
   showed real accumulated state on the **same long-lived local Redis
   container this whole session had reused across 25+ pytest invocations
   without ever restarting it** — an artifact of this sandbox's own usage
   pattern today, not a fresh production Redis instance's behavior.
   Restarted the local Redis container; the exact same isolated test then
   passed 5-of-5 clean.
5. **Final confirmation run, after both fixes and the Redis restart:
   1157 passed, 1 failed** — `test_worker.py::test_run_active_rooms_is_
   safe_to_call_repeatedly`, the one flake independently confirmed via a
   `git stash` A/B test earlier this session to reproduce identically on
   the original, unmodified commit (`a665463`) with none of this
   session's changes present — pre-existing, not introduced by this pass,
   not by anything in the Redis fix. Re-run 8x in isolation immediately
   after this result to characterize it on its own terms (see the
   dedicated finding below rather than left as an unresolved loose end).
   Runtime: 10:10, back to the normal range. Zero `MaxConnectionsError`,
   zero generic Redis timeouts elsewhere in this run.

**Net result: the systemic issue is fixed. A narrower, pre-existing,
test-specific issue remains, characterized rather than hand-waved.**
Immediately after the clean full-suite run above,
`test_run_active_rooms_is_safe_to_call_repeatedly` was run 8x back to
back in isolation to characterize it on its own terms: it failed with
the same `MaxConnectionsError`, even at the raised 200 cap, while a
direct `redis-cli info clients` check *at that same moment* showed only
1 real connected client on the server — proving the exhaustion is
something that happens **within this one test's own single-process
lifetime** (a fresh client pool per pytest invocation can't accumulate
leaked connections *across* runs the way the earlier systemic bug did),
not a symptom of lingering server-side state this time.

This narrows the finding precisely: this specific test's own exercise
pattern (rapid `run_active_rooms()` calls plus `EngineWorker.shutdown()`
cancelling every owned engine's tasks) can, on its own, occasionally
drive connection usage high enough to matter — a real, if narrow,
connection-handling sensitivity in `RoundEngine`/`room_lock.py`'s
interaction with task cancellation, not fully resolved by this pass's
fix. It was already independently confirmed via a `git stash` A/B test
earlier this session to fail identically on the original, unmodified
commit (`a665463`) with none of this session's changes present — **it
predates this entire pass** and this pass's fix measurably narrowed its
blast radius (from "randomly hits any of 1150+ tests on nearly every
full-suite run" to "occasionally hits this one specific test under rapid
repeated invocation") without fully eliminating it. No evidence this
represents a real *production* risk: production runs one long-lived
`engine-worker` process with one persistent pool, never this test's
artificial back-to-back-fresh-process pattern, and the test itself
verifies task bookkeeping (no duplicate engine per room), not any
financial code path. Recommended as a real, scoped follow-up
investigation into `room_lock.py`'s Lua-script cancellation handling —
not a launch blocker, and not left unexplained.

mypy: clean (0 errors, 102 source files) at every checkpoint during this
pass, including after every fix above.
