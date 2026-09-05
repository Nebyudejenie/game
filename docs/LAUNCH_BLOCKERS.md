# Launch Blockers

The single, structured list of everything standing between current `main`
and a real-money public launch. Built from `docs/PRODUCTION_READINESS.md`'s
evidence-based audit — this file doesn't re-derive that evidence, it turns
each RED/YELLOW/UNVERIFIED finding into one executable item with an owner
and a verification command.

**Category legend** (what kind of blocker this is, not how serious it is —
severity is the separate `Priority` field):

- **A** — AI can fix directly in this repo, right now. No human action
  needed beyond reviewing and merging the change.
- **B** — This environment cannot reach what's needed (production host,
  Cloudflare dashboard, Alertmanager config), but everything an operator
  needs — exact commands, prepared files, expected output — is ready.
  A human with the right access executes one deterministic procedure.
- **C** — Requires a physical device, a real Telegram account, or real
  money movement. No sandbox substitutes for this category by
  construction.
- **D** — Requires a decision or authority outside engineering: legal
  counsel, a regulator, Telegram's own platform policy, or a business
  call only the product owner can make.

**Priority**: P0 (blocks any public real-money launch) → P3 (track, not
blocking).

---

## Category A — AI can fix now

### LB-A1: Ledger-level reconciliation has no schedule wired

- **Priority**: P0 → done
- **Area**: Financial integrity
- **Problem**: `packages/core/ledger.py::reconcile()` / `packages/core/
  reconcile_job.py` are real, tested functions, but nothing in this repo
  calls them on a timer. Provider-level reconciliation (Telebirr/Chapa)
  *is* scheduled inside `payout_worker.py`; ledger-level reconciliation
  (unbalanced entries, orphan records, negative balances, duplicate
  idempotency keys) is not.
- **Why it matters**: A real-money ledger with no periodic self-check
  means a bug that silently unbalances an account could run for days
  before anyone notices — the difference between catching a ETB 50
  discrepancy same-day versus discovering a ETB 50,000 one at audit time.
- **Can AI fix it?**: Yes. `payout_worker.py::main_async()` already runs
  5 independent periodic sweeps via `asyncio.create_task(_run_periodic_
  sweep(name, interval, fn))` — the exact pattern to reuse, not a new
  scheduler.
- **Required human action**: Review and merge; confirm the chosen
  interval (proposed: hourly, matching the existing provider
  reconciliation cadence) is acceptable.
- **Exact verification command**:
  `docker compose -f deploy/docker-compose.yml logs payments | grep reconcile_job_run`
  (or the equivalent prod compose file) — should show a run at least once
  per configured interval.
- **Evidence required**: A log line per run showing entries checked,
  discrepancies found (ideally 0), and duration; a real test that seeds a
  known-bad row and confirms the job detects (not silently fixes) it.
- **Current status**: **Done.** `services/payments/ledger_reconcile_sweep.py`
  added, wired as a 7th task in `payout_worker.py::main_async()` via the
  existing `_run_periodic_sweep()` pattern (hourly, matching the two
  existing provider-reconciliation sweeps' cadence). New scraped gauge
  `ledger_reconciliation_sweep_mismatch_count` on the default registry
  (payout-worker's already-scraped `/metrics`), new alert rule
  `LedgerReconciliationSweepMismatch` in `deploy/prometheus/alerts.yml`.
  Two real tests added (`tests/integration/test_ledger_reconcile_sweep.py`):
  a clean ledger sweeps to 0, and a deliberately tampered
  `account_balances` row is detected and logged but never silently
  corrected. mypy clean.
- **Owner**: Engineering (this repo).

### LB-A2: Zero-player spectator mode

- **Priority**: P3 (informational — verified, no gap found)
- **Area**: Bingo engine / lobby
- **Problem**: Needed confirmation a room with 0 players stays visibly
  "live" in the lobby list rather than disappearing or erroring.
- **Why it matters**: A room that looks broken with 0 players (rather
  than "waiting for players") reads as a bug to the first user who opens
  it, and undermines confidence at the exact moment a new player is
  deciding whether the platform works.
- **Can AI fix it?**: N/A — verified correct; a real test gap (not a code
  gap) was found and closed.
- **Required human action**: None.
- **Exact verification command**: `pytest tests/integration/test_gateway_gameplay.py::test_a_zero_player_room_still_appears_live_in_the_room_list -q`
- **Evidence required**: The passing test (added this pass).
- **Current status**: **Verified now.** `services/gateway/queries.py::
  list_rooms()` has no player-count filter (`SELECT ... LEFT JOIN
  LATERAL ... count(DISTINCT user_id) ...`, defended with `or 0`), and
  `RoundEngine.run_forever()` proactively opens a room's first round the
  instant its engine claims it, so a fresh, empty room is never left at a
  bare, round-less "idle" — it shows `status: "lobby"`, `players: 0`, and
  a real countdown. No existing test covered this exact case (existing
  coverage always joined a player first); added
  `test_a_zero_player_room_still_appears_live_in_the_room_list` to
  `tests/integration/test_gateway_gameplay.py`, asserting a real WS
  client sees a 0-player room with `status="lobby"` and a live
  `lobby_deadline_ms`. Passed on the first run — confirms the code, not a
  fix.
- **Owner**: Engineering (this repo) — closed.

### LB-A3: Card-assignment requirement ("0–420") — re-verified, the premise itself was outdated

- **Priority**: P3 (informational — no gap found)
- **Area**: Bingo engine
- **Problem**: This directive's own premise ("0–420") does not match the
  current, correct, already-decided requirement. Checked directly against
  both the live database and this repo's own decision record rather than
  assumed either way.
- **Why it matters**: Verifying against the actual requirement, not a
  possibly-stale figure carried over between directives, is exactly what
  this item asks for — and in this case the figure itself needed
  correcting, not the code.
- **Can AI fix it?**: N/A — nothing to fix. Investigated and confirmed
  already correct.
- **Required human action**: None. For awareness only: `DECISIONS.md`'s
  "2026-09-03 — Card pool corrected: 150 was wrong, true size is 432"
  entry documents that an earlier "0–420 (421 positions)" claim was
  itself investigated and disproven by direct frame-by-frame review of
  the reference video (four independent frames showing the grid's real
  last row as "...431, 432"; four real in-video "Card #" sightings — 194,
  403, 126, 407 — all falling within 1–432 and none supporting 420/421),
  personally verified before acting on it, not taken from a sub-agent
  report alone.
- **Exact verification command**: `SELECT count(*), min(card_no), max(card_no) FROM cards;`
- **Evidence required**: The query result.
- **Current status**: **Verified now**, live: `count=432, min=1, max=432`
  — matches `DECISIONS.md`'s recorded correction exactly.
  `packages/core/bingo.py::_POOL_SIZE = 432`;
  `services/engine/round_engine.py::load_card_pool()` loads dynamically
  from the `cards` table (no hardcoded count on the engine side).
  Existing tests pin the first 100 and first 150 entries' hashes to
  prove the append-only expansion never altered any existing card_no's
  grid.
- **Owner**: Engineering (this repo) — closed.

### LB-A4: Bingo engine acceptance list — audited item-by-item against all 16 named scenarios

- **Priority**: P1 (5 of 16 closed this pass; 3 real gaps remain, downgraded to P2 — see below)
- **Area**: Bingo engine
- **Problem**: A specific 16-scenario win/claim acceptance list needed
  item-by-item verification against existing tests.
- **Why it matters**: This is the platform's core money-moving logic; an
  unlisted gap here is a direct payout-correctness risk.
- **Can AI fix it?**: Yes.
- **Exact verification command**: `pytest tests/unit/test_bingo.py tests/integration/test_round_engine.py tests/integration/test_gateway_gameplay.py -q`
- **Current status — full scorecard**:
  1. One row rejected — already covered.
  2. One column rejected — **gap found, closed**: new
     `test_has_won_false_with_only_one_complete_column`.
  3. One diagonal rejected — **gap found, closed**: new
     `test_has_won_false_with_only_one_complete_diagonal`.
  4. Two rows accepted — already covered.
  5. Two columns accepted — **gap found, closed**: new
     `test_has_won_true_for_two_columns`.
  6. Row + column accepted — already covered.
  7. Row + diagonal accepted — already covered.
  8. Column + diagonal accepted — **gap found, closed**: new
     `test_has_won_true_for_column_plus_diagonal`.
  9. Two diagonals accepted — already covered.
  10. FREE-space combinations — already covered.
  11. Duplicate claim rejected — already covered
      (`test_same_user_double_claim_race_settles_exactly_once`).
  12. Concurrent claims, multiple players, exactly one winner — **partial
      gap, not closed this pass**: existing coverage proves (a) a
      same-card race settles exactly once, and (b) two *different*,
      genuinely-winning players are both correctly paid (this product's
      real tie-window design, not a bug). No test yet races two different
      players where only *one* actually holds a valid pattern. Downgraded
      to P2 — the underlying single-winner-vs-tie logic is exercised, just
      not this exact combination.
  13. Reconnect-during-claim doesn't double-process — **gap, not closed
      this pass**. Existing chaos tests cover reconnect generally, not
      concurrent with an in-flight claim specifically. P2.
  14. Stale claim rejected — mostly covered (premature claim, claim before
      round running); the specific "claim arrives after the round already
      settled" path has no dedicated test. P2.
  15. Malformed claim rejected gracefully — **gap found, closed**: new
      `test_a_malformed_claim_is_rejected_gracefully_not_crashed` proves a
      non-integer `round_id` gets a clean `bad_round_id` error over a real
      WS connection, and the connection stays alive afterward.
  16. Unauthorized claim rejected — covered, but the existing fixture was
      weak (claimed an unheld card, not one someone else genuinely holds)
      — **strengthened**: new
      `test_claim_for_a_card_someone_else_holds_is_rejected` has a real
      third party claim a card a real, in-round player actually holds.
- **Evidence required**: The scorecard above; all listed tests added to
  `tests/unit/test_bingo.py`, `tests/integration/test_round_engine.py`,
  `tests/integration/test_gateway_gameplay.py`, passing.
- **Required human action**: None for the 5 closed items. For the 3
  remaining (12, 13, 14's settled-claim path): no product ambiguity, just
  unbuilt test scenarios — schedule as scoped follow-up work.
- **Owner**: Engineering (this repo).

### LB-A5: Adversarial wallet/financial test sweep

- **Priority**: P3 (informational — verified, no gap found)
- **Area**: Wallet / ledger
- **Problem**: Needed confirmation that (a) no balance-mutation path
  anywhere bypasses the ledger, and (b) duplicate/concurrent/replay
  conditions are genuinely exercised for the main money-moving paths, not
  just assumed from the ledger's own primitive-level tests.
- **Why it matters**: Confirms defense-in-depth for the platform's single
  highest-consequence property: money can't be duplicated or invented.
- **Can AI fix it?**: N/A — verified, no gap found.
- **Required human action**: None.
- **Exact verification command**: `grep -rn "UPDATE accounts\|UPDATE account_balances\|SET balance" services/ packages/ migrations/ --include="*.py" | grep -v packages/core/ledger.py`
- **Evidence required**: The grep output.
- **Current status**: **Verified now.** The grep above returns nothing —
  every balance mutation in the entire codebase (application code and
  every migration) goes through `ledger.post()`, with no exception.
  Adversarial conditions are already exercised, not just theorized:
  `test_same_webhook_delivered_100_times_concurrently_credits_exactly_once`
  (deposit replay), `test_concurrent_withdrawal_and_stake_never_both_
  succeed` (adversarial race between a withdrawal and a stake on the same
  balance), `test_concurrent_stakes_exactly_half_succeed` and
  `test_same_idempotency_key_fired_concurrently_100_times` (ledger-level
  concurrency/idempotency), plus this pass's own new
  `test_sweep_detects_a_mismatch_without_correcting_it` (LB-A1) proving a
  tampered cache is surfaced, never silently reconciled away.
- **Owner**: Engineering (this repo) — closed.

### LB-A6: Dedicated Telegram security audit document

- **Priority**: P1 → done
- **Area**: Security / Telegram
- **Problem**: initData validation, auth freshness, replay protection,
  and the "server never trusts client-provided balance/user id/wallet
  amount/claim status" invariant needed to be verified end to end and
  written up as one dedicated, reviewable document.
- **Why it matters**: A real-money Mini App's single biggest attack
  surface is a forged or replayed client message.
- **Can AI fix it?**: Yes — done.
- **Required human action**: Review `docs/TELEGRAM_SECURITY_AUDIT.md`;
  note its one YELLOW (no explicit WS `Origin` header check, mitigated by
  the `initData` requirement) and its UNVERIFIED (real-device/WebView
  behavior, tracked as LB-C1).
- **Exact verification command**: N/A (documentation).
- **Evidence required**: `docs/TELEGRAM_SECURITY_AUDIT.md`, written this
  pass with real file:line citations for every claim (confirmed
  `self._user_id` is set once at auth time from the server-validated
  Telegram id and read — never re-derived from client input — at all 12
  use sites in `connection.py`; confirmed no third, undiscovered
  bot-token comparison exists anywhere else in the codebase).
- **Current status**: Done.
- **Owner**: Engineering (this repo) — closed.

### LB-A7: `docs/DISASTER_RECOVERY_DRILL.md` (scenario runbook)

- **Priority**: P1 → done
- **Area**: Operations
- **Problem**: `docs/DISASTER_RECOVERY.md` (mechanism-level) existed; a
  scenario-driven drill doc across all 7 named scenarios did not.
- **Why it matters**: A runbook read for the first time during a real
  incident is a runbook that fails; this needs to exist and be
  rehearsable before it's needed.
- **Can AI fix it?**: Yes — done. RPO/RTO numbers that depend on an
  unwired backup schedule are explicitly marked as such (linked to
  LB-B2/LB-B3) rather than invented.
- **Required human action**: Review; walk each scenario's "Drilled?" line
  for real on production once access exists, and record the outcome.
- **Exact verification command**: N/A (documentation).
- **Evidence required**: `docs/DISASTER_RECOVERY_DRILL.md`, written this
  pass, covering DB corruption / server loss / Redis loss / app restart /
  payment-worker interruption / Bingo-worker interruption / Cloudflare
  Tunnel interruption, each with real commands and an honest "not yet
  drilled" where true.
- **Current status**: Done.
- **Owner**: Engineering (this repo) — closed; real drills still needed
  on production (tracked via LB-B3/LB-B4/LB-B6).

### LB-A8: `docs/RESPONSIBLE_GAMING_REQUIREMENTS.md`

- **Priority**: P1 → done (technical columns)
- **Area**: Responsible gaming / legal
- **Problem**: Technical controls needed mapping against legal
  requirements, which are themselves not yet known (see LB-D2).
- **Why it matters**: Separates "what the code does" from "what the law
  requires," so nothing gets silently assumed compliant.
- **Can AI fix it?**: Partially — done. TECHNICAL CONTROL / CURRENT
  IMPLEMENTATION / EVIDENCE columns fully populated (5 real, server-side-
  enforced controls: age declaration, self-exclusion with a 180-day
  minimum and no self-reversal, cool-off, deposit limits, loss limits —
  all instant-decrease/24h-delayed-increase where applicable; plus a
  client-side session-time reminder and running net-position display).
  Also found 3 real gaps worth a future product decision: no
  platform-enforced maximum limit ceiling, no independent age
  verification beyond self-declaration, no in-app problem-gambling
  resource links. LEGAL columns correctly left UNKNOWN pending LB-D2.
- **Required human action**: Legal review (LB-D2) to fill the remaining
  columns; a product decision on the 3 flagged gaps once that review
  clarifies what's actually required.
- **Exact verification command**: N/A (documentation).
- **Evidence required**: `docs/RESPONSIBLE_GAMING_REQUIREMENTS.md`.
- **Current status**: Done (technical); legal columns blocked on LB-D2.
- **Owner**: Engineering (technical columns), qualified local counsel
  (legal columns).

### LB-A9: `docs/PLATFORM_POLICY_REVIEW.md`

- **Priority**: P1 → done (technical/business columns)
- **Area**: Platform policy
- **Problem**: Needed a document separating TECHNICAL CAPABILITY from
  PLATFORM POLICY from LEGAL REQUIREMENT from BUSINESS DECISION.
- **Why it matters**: Prevents a technical capability ("we can do X")
  from being mistaken for permission ("Telegram/the law allows X").
- **Can AI fix it?**: Partially — done. TECHNICAL CAPABILITY and BUSINESS
  DECISION columns fully populated across 6 rows (real-money movement,
  in-Mini-App wallet, promotional messaging, the referral cash program,
  phone-number collection, and non-Telegram-native payment rails).
  PLATFORM POLICY / LEGAL REQUIREMENT correctly left UNKNOWN pending
  LB-D1/LB-D2. The document explicitly names one load-bearing question
  worth resolving first: whether real-money gambling via external payment
  rails is permitted under Telegram's platform policy at all — every
  other row is downstream of that answer.
- **Required human action**: Platform policy review (LB-D1), prioritizing
  the one load-bearing question above.
- **Exact verification command**: N/A (documentation).
- **Evidence required**: `docs/PLATFORM_POLICY_REVIEW.md`.
- **Current status**: Done (technical/business); policy/legal columns
  blocked on LB-D1/LB-D2.
- **Owner**: Engineering (technical column), product owner (business
  decision column), platform-policy reviewer (policy column).

### LB-A10: `docs/RELEASE_CANDIDATE.md`

- **Priority**: P1 → done
- **Area**: Release management
- **Problem**: Needed a single document recording the exact state of this
  candidate build.
- **Why it matters**: A "what exactly did we ship" record for rollback or
  incident investigation.
- **Can AI fix it?**: Yes — done, every field traced to a real command.
- **Required human action**: Review before tagging a real release; commit
  the working tree this record describes (done as part of this pass's
  final commit).
- **Exact verification command**: N/A (documentation).
- **Evidence required**: `docs/RELEASE_CANDIDATE.md` — records the
  1169-passed/0-failed final run and mypy-clean result.
- **Current status**: Done.
- **Owner**: Engineering (this repo) — closed.

### LB-A11: `docs/FINAL_LAUNCH_ACCEPTANCE.md` (the 29-area PASS/FAIL/BLOCKED/UNKNOWN table)

- **Priority**: P0 → done
- **Area**: Release management
- **Problem**: Needed one canonical go/no-go artifact in the required
  PASS/FAIL/BLOCKED/UNKNOWN/N/A shape across all 29 named areas.
- **Why it matters**: A single canonical artifact, not several
  overlapping documents an operator has to reconcile by hand.
- **Can AI fix it?**: Yes — done, re-projected from this pass's own
  updated evidence (not the stale pre-pass state).
- **Required human action**: Final review before the GO/NO-GO call.
- **Exact verification command**: N/A (documentation).
- **Evidence required**: `docs/FINAL_LAUNCH_ACCEPTANCE.md` — roll-up: 18
  PASS, 1 FAIL (#23 HTTPS redirect), 7 BLOCKED, 2 UNKNOWN, 1 N/A.
- **Current status**: Done. Overall call: **NOT YET GO** (one confirmed
  FAIL, one near-FAIL backup gap, two unresolved legal/policy UNKNOWNs).
- **Owner**: Engineering (this repo), final sign-off by product owner.

---

## Category B — AI cannot reach it, but has prepared everything an operator needs

### LB-B1: HTTP → HTTPS redirect not enabled

- **Priority**: P0
- **Area**: Infrastructure / Cloudflare
- **Problem**: `docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` documents that
  "Always Use HTTPS" (or an equivalent redirect rule) was never
  configured at the Cloudflare zone level.
- **Why it matters**: A real-money app serving any traffic over plain
  HTTP exposes session tokens and payment flows to trivial
  network-level interception.
- **Can AI fix it?**: No — requires the Cloudflare dashboard or an
  API token with zone-edit rights, neither available in any session.
- **Required human action**: In the Cloudflare dashboard for the zone:
  SSL/TLS → Edge Certificates → enable "Always Use HTTPS" (or add a
  redirect Page Rule `http://*arada.fun/*` → `https://$1`).
- **Exact verification command**:
  `curl -sI http://arada.fun/ | head -1` — expect a `301`/`308` to
  `https://arada.fun/`, not a `200`.
- **Evidence required**: The actual `curl` output showing the redirect,
  run from a machine with real internet access to the public domain.
- **Current status**: Unresolved as of the last check (2026-09-05).
- **Owner**: Whoever holds the Cloudflare account for `arada.fun`.

### LB-B2: Backup schedule not wired anywhere

- **Priority**: P0
- **Area**: Disaster recovery
- **Problem**: `backup.sh`/`basebackup.sh`/`prune_wal_archive.sh` are
  real, tested scripts (`test_backup_restore.py` proves the round trip
  works), but no cron/systemd-timer invokes any of them anywhere in this
  repo or (as far as can be determined without production access) on the
  production host.
- **Why it matters**: A backup mechanism that works but never runs is
  operationally identical to having no backups.
- **Can AI fix it?**: Partially — done: real, ready-to-install systemd
  timer/service unit files now exist in this repo
  (`deploy/systemd/jobingo-{backup,basebackup,prune-wal}.{service,timer}`
  + `deploy/systemd/README.md`); cannot install or enable them on the
  production host without SSH access.
- **Required human action**: Confirm the deploy path placeholder in the 3
  `.service` files is still correct, then copy them to the production
  host and enable them (`deploy/systemd/README.md`'s exact steps).
- **Exact verification command**:
  `systemctl list-timers | grep jobingo` — expect three listed timers
  with a real `NEXT`/`LAST` run time.
- **Evidence required**: The `systemctl list-timers` output, plus a real
  backup file's timestamp/size/checksum from the production backup
  directory, no older than the configured interval.
- **Current status**: Unit files prepared and syntax-validated
  (`systemd-analyze verify` — clean except for the expected "file doesn't
  exist in this sandbox" errors against the real production path); not
  installed anywhere yet.
- **Owner**: Whoever holds SSH access to the production host.

### LB-B3: Restore never drilled against a real production backup

- **Priority**: P1
- **Area**: Disaster recovery
- **Problem**: `restore.sh`/`restore_pitr.sh` are tested against
  synthetic local data; never run against an actual production backup
  file.
- **Why it matters**: "The script works on fake data" is not the same
  claim as "we can actually recover production" — restore procedures are
  notorious for failing on real-world edge cases synthetic data doesn't
  hit (encoding, size, extension availability).
- **Can AI fix it?**: No — needs a real production backup file and a
  disposable instance to restore it onto (never production itself).
- **Required human action**: Copy the latest real backup to a disposable
  Postgres instance, run `restore.sh` against it, record
  timestamp/size/checksum/duration/result.
- **Exact verification command**:
  `./deploy/restore.sh <backup-file> && psql -c "SELECT count(*) FROM users;"`
  against the disposable instance — expect a plausible, non-zero row
  count matching production's real scale.
- **Evidence required**: The restore log, duration, and the row-count
  check's output.
- **Current status**: Not performed.
- **Owner**: Whoever holds access to the production backup store.

### LB-B4: Production deployment state (does prod actually run current `main`?) unconfirmed

- **Priority**: P0
- **Area**: Infrastructure
- **Problem**: Local `main` == `origin/main`; whether the production
  host (`cosmic@192.168.1.173`, a private LAN address, confirmed
  unreachable via `git ls-remote prod`) has pulled and deployed any of it
  is unknown from this environment.
- **Why it matters**: Every other finding in this document assumes the
  code being audited is the code actually running for real users — that
  assumption itself needs to be confirmed, not inferred.
- **Can AI fix it?**: No — no network path from this sandbox to that LAN
  address.
- **Required human action**: On the production host itself (or a
  machine on that LAN): `git log -1 --oneline` and compare to this
  repo's current `HEAD`.
- **Exact verification command**: `ssh cosmic@192.168.1.173 'cd /home/cosmic/game && git log -1 --oneline'`
- **Evidence required**: The commit hash, matched against
  `git rev-parse HEAD` run in this repo at the same time.
- **Current status**: Unconfirmed.
- **Owner**: Whoever holds SSH access to the production host.

### LB-B5: Alerting has rules but no confirmed real receiver

- **Priority**: P0
- **Area**: Monitoring
- **Problem**: 8 real Prometheus alert rules exist
  (`deploy/prometheus/alerts.yml`); whether Alertmanager has a real
  receiver (Slack/PagerDuty/etc.) configured in production, and whether
  it actually pages a real person, has never been confirmed end to end.
- **Why it matters**: A firing alert with nowhere to go is
  indistinguishable from no monitoring at all during a real incident.
- **Can AI fix it?**: No — requires production Alertmanager config
  access and a real, controlled test firing.
- **Required human action**: Trigger one real alert condition
  (deliberately, in a controlled way) and confirm a human actually
  receives the page; then resolve it and confirm the resolution
  notification also arrives.
- **Exact verification command**:
  `amtool alert add alertname=LaunchBlockersDrill severity=critical --alertmanager.url=<url>`
  then confirm receipt, then
  `amtool silence add alertname=LaunchBlockersDrill --alertmanager.url=<url>`
  to resolve it.
- **Evidence required**: A screenshot or log of the actual received
  page/message, timestamped, plus its resolution.
- **Current status**: Unconfirmed.
- **Owner**: Whoever administers the production Alertmanager/on-call
  tooling.

### LB-B6: Cloudflare/Traefik live state stale relative to current HEAD

- **Priority**: P1
- **Area**: Infrastructure
- **Problem**: All 5 production hostnames were verified live directly
  against production in an earlier session (2026-09-05), but those docs
  are several commits behind current `HEAD` (don't cover this session's
  newest admin routes, though those live inside the already-verified
  `admin.arada.fun` container).
- **Why it matters**: Confirms nothing has drifted (DNS, TLS, tunnel
  health) since the last real check.
- **Can AI fix it?**: No — no network path to the public domains from
  this sandbox to re-verify live.
- **Required human action**: Re-run the exact external checks below from
  a machine with real internet access.
- **Exact verification command** (run for each of `arada.fun`,
  `payments.arada.fun`, `admin.arada.fun`, `finance.arada.fun`,
  `agent.arada.fun`):
  `curl -sI https://<host>/healthz` — expect `200`, a valid certificate,
  and `server: cloudflare` in the response headers.
- **Evidence required**: The `curl` output for all 5 hostnames.
- **Current status**: Verified as of 2026-09-05; not re-checked since.
- **Owner**: Whoever has real network access to the public domains.

### LB-B7: Load testing never run against production-like infrastructure

- **Priority**: P1
- **Area**: Performance
- **Problem**: No load test has been run against anything resembling
  production's real infrastructure — this sandbox's dev stack isn't
  representative.
- **Why it matters**: Confirms the platform survives real concurrent
  traffic (many simultaneous rooms/players) before it's asked to for
  real.
- **Can AI fix it?**: Partially — can prepare a real load-test script
  (e.g. targeting the WS join/claim path and the REST deposit path) now;
  cannot run it meaningfully without production-like infrastructure.
- **Required human action**: Run the prepared load test against a
  staging environment sized like production.
- **Exact verification command**: (script to be prepared) —
  `locust -f loadtest/bingo_load.py --host=https://staging.arada.fun`.
- **Evidence required**: Latency/error-rate results at a defined
  concurrent-user target.
- **Current status**: Not started.
- **Owner**: Whoever controls a staging environment.

---

## Category C — Requires a physical device, real Telegram, or real money

### LB-C1: Real-world Telegram/Mini App smoke test

- **Priority**: P0 (before any public launch)
- **Area**: Telegram / Mini App
- **Problem**: Code-level behavior (initData validation, WS reconnect,
  UI) is verified locally; real behavior under a real Telegram WebView,
  real mobile network conditions, and real Telegram client versions has
  not been observed.
- **Why it matters**: Telegram's WebView and real mobile networks
  introduce failure modes (backgrounding, connection drops, clock skew)
  no local test can reproduce.
- **Can AI fix it?**: No.
- **Required human action**: Open the real bot in a real Telegram
  client on a real phone; play a full round end to end (join → play →
  claim → see payout); background and foreground the app mid-round;
  force a network drop and confirm WS reconnect.
- **Exact verification command**: N/A — manual checklist.
- **Evidence required**: A completed checklist with observed results
  for each step (screen recording recommended).
- **Current status**: Not performed.
- **Owner**: Product owner or a designated real-device tester.

### LB-C2: Controlled real-money payment test (Chapa)

- **Priority**: P0 (before enabling any live payment rail at scale)
- **Area**: Payments
- **Problem**: Deposit/withdrawal code is real and tested against
  Chapa's sandbox/test mode; a real, small, controlled real-money
  transaction has not been run through it.
- **Why it matters**: Confirms the real production credentials, real
  webhook delivery, and real settlement timing all actually work — a
  sandbox pass doesn't guarantee this.
- **Can AI fix it?**: No.
- **Required human action**: Perform one small real deposit and one
  small real withdrawal with real money, confirm correct ledger
  entries and correct balance, then confirm a support/reversal path if
  needed.
- **Exact verification command**: N/A — manual, with a follow-up
  `SELECT * FROM ledger_transactions WHERE ... ORDER BY id DESC LIMIT 5;`
  to confirm the resulting entries.
- **Evidence required**: The transaction receipt/reference plus the
  matching ledger rows.
- **Current status**: Not performed.
- **Owner**: Product owner, with real payment credentials.

### LB-C3: Telebirr SMS controlled real-world test

- **Priority**: P0 (before enabling `payment_provider_availability.telebirr_sms`)
- **Area**: Payments / Telebirr
- **Problem**: The full SMS ingestion/parsing/redemption/reconciliation
  pipeline is real and tested; `telebirr_sms` remains deliberately
  disabled pending a real SMS from a real Telebirr transaction through a
  real Android phone running MacroDroid.
- **Why it matters**: This is the one payment rail whose correctness
  depends on a physical device forwarding real carrier SMS content —
  nothing about that can be verified without one.
- **Can AI fix it?**: No.
- **Required human action**: Follow `docs/TELEBIRR_PRODUCTION_CHECKLIST.md`
  and `docs/TELEBIRR_MACRODROID_QUICK_SETUP.md` end to end with a real
  device and a real small transaction; only then flip
  `payment_provider_availability.telebirr_sms = true`.
- **Exact verification command**: N/A — physical procedure, documented
  in the referenced checklists.
- **Evidence required**: A completed checklist plus the real ingested
  SMS's resulting ledger entry.
- **Current status**: Correctly kept OFF pending this. Not performed.
- **Owner**: Product owner, with a real Android phone and Telebirr
  account.

### LB-C4: MacroDroid physical-device checklist completeness

- **Priority**: P1 (blocks LB-C3)
- **Area**: Payments / Telebirr
- **Problem**: `docs/TELEBIRR_MACRODROID_QUICK_SETUP.md` covers setup;
  needs explicit confirmation it addresses device-ID binding, delivery
  retry, de-duplication, reboot survival, battery-optimization
  exemption, and that the bearer token never appears in MacroDroid's own
  logs.
- **Why it matters**: A misconfigured phone (e.g., battery optimization
  killing the forwarding app) silently breaks the entire Telebirr rail
  without any code-level symptom.
- **Can AI fix it?**: Partially — can review and tighten the document's
  text now; cannot verify actual on-device behavior.
- **Required human action**: Walk the real checklist on the real device
  once, confirming each named property.
- **Exact verification command**: N/A — physical procedure.
- **Evidence required**: Confirmation of each named property against
  the real device.
- **Current status**: Document exists; on-device confirmation not
  performed.
- **Owner**: Product owner, with the real device.

---

## Category D — Requires a decision or authority outside engineering

### LB-D1: Telegram platform policy review

- **Priority**: P0
- **Area**: Legal / platform policy
- **Problem**: Whether real-money wagering inside a Telegram Mini
  App/Bot complies with Telegram's current Bot API / Mini App / payments
  policies has not been reviewed — this is a policy judgment, not a code
  audit.
- **Why it matters**: A platform-policy violation can result in the bot
  being banned outright, independent of how correct the code is.
- **Can AI fix it?**: No — this session has no authority to certify
  platform-policy compliance.
- **Required human action**: The product owner (or counsel) reviews
  Telegram's current live Bot API/Mini App terms against this specific
  product.
- **Exact verification command**: N/A.
- **Evidence required**: A written determination, dated, citing the
  specific policy sections reviewed.
- **Current status**: Not performed.
- **Owner**: Product owner / counsel.

### LB-D2: Legal/regulatory review (real-money gambling)

- **Priority**: P0
- **Area**: Legal
- **Problem**: Real-money Bingo/gambling regulation is
  jurisdiction-specific; no legal review has been performed or is
  performable by this session.
- **Why it matters**: Operating a real-money gambling product without
  confirming legal status in each operating jurisdiction is a business
  and personal-liability risk no amount of engineering work mitigates.
- **Can AI fix it?**: No.
- **Required human action**: Obtain real legal counsel review for every
  jurisdiction this product will operate in, before any public launch.
  This also unblocks LB-A8's legal-requirement columns.
- **Exact verification command**: N/A.
- **Evidence required**: Written legal opinion(s), dated.
- **Current status**: Not performed.
- **Owner**: Product owner / counsel.

### LB-D3: Committed secret in git history — accept residual risk or rewrite history?

- **Priority**: P1 (decision), not P0 (no evidence the exposed value is
  the real production key)
- **Area**: Security
- **Problem**: `PHONE_ENCRYPTION_KEY` lived in `.env.example` across 5
  commits (2026-08-24 to 2026-09-05), removed from `HEAD` but permanently
  recoverable from git history by anyone with an existing clone.
  Confirmed to differ from the real production key.
- **Why it matters**: History-rewriting to purge it (BFG/`git
  filter-repo`) is disruptive and hard to reverse — breaks every existing
  clone/fork/open PR — so this is a real cost/benefit call for the
  product owner, not something to do unilaterally.
- **Can AI fix it?**: No — this is a decision, not a technical task; the
  technical rewrite itself, once decided, is straightforward.
- **Required human action**: Decide: accept the residual risk (low,
  given the confirmed mismatch with the real production key) or schedule
  a coordinated history rewrite with everyone who has a clone.
- **Exact verification command**: `git log --all --oneline -- .env.example | head -20`
  to see the exposure window again if needed.
- **Evidence required**: A written decision, dated.
- **Current status**: Flagged, undecided.
- **Owner**: Product owner.

### LB-D0: Bonus Rules' lifecycle is simpler than a full draft→review→approved→scheduled→paused→expired state machine

- **Priority**: P3
- **Area**: Product scope / bonus safety
- **Problem**: `bonus_rules` (migration `4bbb21e0f5ad`) has `is_active`
  (boolean) plus `starts_at`/`ends_at` — not a named, explicit
  draft/review/approved/scheduled/active/paused/expired lifecycle.
- **Why it matters, and why this might be fine as-is**: A launch-
  readiness review asked for that fuller lifecycle explicitly. The
  current simpler design already gives real protection against the thing
  a fuller lifecycle would prevent — a rule isn't live unless
  `is_active=true` *and* within its start/end window, creating or
  activating one requires `bonuses:manage_rules` (ops+superadmin only),
  and every change is audited. Building a 6-state machine on top of that
  for a feature that's currently well-guarded by three simpler
  primitives risks being exactly the "add features just to look
  advanced" pattern this same review process explicitly warns against
  elsewhere. Flagged as a real scope question, not silently resolved
  either direction.
- **Can AI fix it?**: Only after a scope decision.
- **Required human action**: Decide whether the extra lifecycle states
  (specifically: a "review" step before a rule can go active, distinct
  from just "created but not yet activated") are worth building, or
  whether `is_active` + RBAC + audit already covers the real risk.
- **Exact verification command**: N/A.
- **Evidence required**: A written decision.
- **Current status**: Not decided.
- **Owner**: Product owner.

### LB-D4: Promotions-as-a-unified-entity scope decision

- **Priority**: P2
- **Area**: Product scope
- **Problem**: Bonus Rules + Notification Center's "Announce this rule"
  link cover the functional requirement (configure a reward, announce
  it), but there's no single "Promotion" object with its own
  draft/publish/pause/archive lifecycle spanning both systems.
- **Why it matters**: This is a real product-scope decision (a new
  unifying abstraction), not a bug — building it silently as a side
  effect of a launch-readiness pass would be scope creep the directive
  itself explicitly warns against.
- **Can AI fix it?**: Only after a scope decision — the underlying pieces
  already exist and work.
- **Required human action**: Decide whether this unification is worth
  building as its own piece of work, or whether today's two-system design
  is sufficient for launch.
- **Exact verification command**: N/A.
- **Evidence required**: A written decision.
- **Current status**: Not decided.
- **Owner**: Product owner.

---

## Summary counts

| Category | Count | Done this pass | Still open |
|---|---|---|---|
| A (AI fixes now) | 11 | 10 (LB-A1–LB-A3, LB-A5–LB-A11) | 1 partial (LB-A4: 13/16 scenarios closed, 3 scoped as P2 follow-up) |
| B (prepared, human executes) | 7 | 0 (by construction — needs a human with access) | 7 |
| C (physical/real-money) | 4 | 0 (by construction — needs a device/real money) | 4 |
| D (external authority/decision) | 5 | 0 (by construction — needs a decision outside engineering) | 5 |

Every Category A item this pass could complete without external access,
it did — 10 of 11 fully closed, the 11th (the Bingo acceptance audit)
substantially closed with the remainder explicitly scoped, not hidden.
Categories B/C/D are, by construction, not resolvable by more engineering
work alone — each already carries the exact command or procedure that
closes it the moment the right person runs it. See
`docs/FINAL_LAUNCH_ACCEPTANCE.md` for how these roll up into the overall
GO/NO-GO decision.
