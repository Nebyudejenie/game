# Final Launch Acceptance

One canonical PASS/FAIL/BLOCKED/UNKNOWN/NOT APPLICABLE table across the
29 named areas this launch process tracks. This re-projects the evidence
already gathered in `docs/PRODUCTION_READINESS.md` and
`docs/LAUNCH_BLOCKERS.md` into one fixed shape for the final decision —
it does not re-investigate; where those documents' evidence has changed
since they were written, they remain the source of truth and this table
is re-derived from them, not the other way around.

**Status definitions**:
- **PASS** — verified working, real evidence, from this environment.
- **FAIL** — verified broken, real evidence.
- **BLOCKED** — cannot be verified from this environment (needs
  production access, a physical device, real money, or real Telegram
  interaction); the exact unblocking procedure is in
  `docs/LAUNCH_BLOCKERS.md`.
- **UNKNOWN** — genuinely unknown, not a code question (legal/platform
  policy); tracked in `docs/LAUNCH_BLOCKERS.md` category D.
- **N/A** — not applicable to this product/scope.

| # | Area | Status | Evidence / Blocker reference |
|---|---|---|---|
| 1 | Bingo engine (core rules) | PASS | Real, no stubs; 16-scenario acceptance audit closed 13/16 items with passing tests, 3 remaining scoped as P2 follow-up (`LAUNCH_BLOCKERS.md` LB-A4). |
| 2 | Two-line win condition | PASS | `MIN_WINNING_LINES = 2` confirmed as the actual running logic both claim paths use; the one historical flake tied to this was a test-timing artifact, root-caused and fixed, not an engine bug. |
| 3 | Wallet / balances | PASS | Double-entry ledger, DB-enforced sum-to-zero and non-negative-balance constraints; adversarial concurrency/replay tests pass; confirmed no balance mutation anywhere bypasses `ledger.post()` (LB-A5). |
| 4 | Ledger integrity | PASS | Real-time reconciliation now scheduled (LB-A1, this pass), alertable, read-only by design (never silently "fixes" a mismatch — proven by a real seeded-mismatch test). |
| 5 | Deposits (Chapa) | PASS | Real webhook + poll fallback, shared idempotent crediting path, real duplicate-delivery test. |
| 6 | Withdrawals | PASS | `user_locked` escrow, auto-approve threshold + manual review above it, real adversarial concurrent-race test. |
| 7 | Telebirr (SMS rail) | BLOCKED | Pipeline real and tested; deliberately kept OFF pending a real controlled SMS test with a physical device (`LAUNCH_BLOCKERS.md` LB-C3). Correct current state, not a defect. |
| 8 | MacroDroid (physical device) | BLOCKED | Setup documented; on-device confirmation of every named property (battery-optimization exemption, dedup, reboot survival, no bearer-token-in-logs) not yet walked on a real phone (LB-C4). |
| 9 | Telegram auth (initData) | PASS | Real HMAC validation, constant-time compare, 24h freshness window; dedicated audit this pass (`docs/TELEGRAM_SECURITY_AUDIT.md`) confirms the server-authoritative-identity invariant at every use site. |
| 10 | Mini App (client) | PASS | Real WS-driven client, session-time reality-check reminders, zero-player room handling verified this pass (LB-A2); one YELLOW noted (no explicit WS Origin check, mitigated by the initData requirement). |
| 11 | Notification Center | PASS | Full lifecycle real and tested; crash-safety gap (stuck-`processing` deliveries) and responsible-gambling audience gap both found and closed earlier this engagement. |
| 12 | Bonuses | PASS | Rule-driven, sticky-wallet model (zero changes to `round_engine.py`), fraud guards (self-referral, shared-payout-account, DB-enforced one-reward-per-referee), 10-way concurrency-tested. |
| 13 | Referrals | PASS | Same engine as Bonuses; real end-to-end webhook-confirmation test; deposit-triggered grants confirmed safe against self-excluded/banned/cooling-off users. |
| 14 | Promotions (as a unified concept) | N/A | Not built as a single named entity distinct from Bonus Rules + a Notifications link — a real, deliberate product-scope decision, not a gap (`LAUNCH_BLOCKERS.md` LB-D4). The *functional* requirement (configure a reward, announce it) is fully covered by what exists. |
| 15 | Admin console | PASS | 79 real routes, no stubs, RBAC covers every permission with verified 200/403 boundaries both directions. |
| 16 | Finance console | PASS | Manual deposit/withdrawal approval, two-person gate above threshold, fully audited. |
| 17 | Payment Agents | PASS | Separate Telegram-delivered auth, narrower data exposure than admin roles by design, real activity visibility. |
| 18 | Monitoring (metrics) | PASS | 18 real Prometheus metrics, Grafana dashboards provisioned; this pass added a new scraped gauge for ledger reconciliation. |
| 19 | Alerting (paging) | BLOCKED | 9 real alert rules now defined (8 pre-existing + 1 new this pass); whether a real receiver pages an actual person in production has never been confirmed end to end (LB-B5). |
| 20 | Backups (schedule) | BLOCKED / FAIL-adjacent | Mechanism real and tested; **no schedule confirmed wired anywhere**, repo-local or production (LB-B2). This is the one item in this table closest to an outright FAIL rather than merely BLOCKED — the gap isn't "can't verify," it's "almost certainly not happening yet." |
| 21 | Restore | BLOCKED | Mechanism real and tested against synthetic data; never drilled against a real production backup (LB-B3). |
| 22 | Cloudflare / DNS / TLS routing | BLOCKED (stale-but-was-PASS) | All 5 hostnames verified live directly against production in an earlier session; docs are several commits stale relative to current `HEAD` (LB-B6) — re-verification needs real network access this environment doesn't have. |
| 23 | HTTP → HTTPS redirect | FAIL | Confirmed, documented gap: never configured at the Cloudflare zone level (LB-B1). This is a real, outstanding P0 defect, not merely unverified. |
| 24 | WebSocket | PASS (code-level); BLOCKED (real-device) | Real HMAC-authenticated handshake, DB-backed stateless resync; real Telegram WebView behavior not verified from this environment (LB-C1). |
| 25 | Security (platform-wide) | PASS, with 1 tracked decision | RBAC/session/rate-limiting/IP-allowlist all real and verified; one historical secret exposure in git history flagged as a pending accept-risk-or-rewrite-history decision (LB-D3), not silently resolved either way. |
| 26 | Responsible gaming | PASS (technical); UNKNOWN (legal) | 5 real, server-side-enforced controls plus a client-side reality check, fully documented in `docs/RESPONSIBLE_GAMING_REQUIREMENTS.md`; whether they meet actual legal requirements is UNKNOWN pending counsel (LB-D2), and 3 real gaps (no platform-enforced limit ceiling, no independent age verification, no in-app help-resource links) are flagged for a product decision once that review lands. |
| 27 | Legal / regulatory review | UNKNOWN | Not performed, not performable by this session (LB-D2). No engineering work substitutes for this. |
| 28 | Telegram platform policy | UNKNOWN | Not performed (LB-D1). One load-bearing question identified and flagged as worth resolving first: whether this product's real-money-via-external-rails design is permitted at all. |
| 29 | Rollback procedure | PASS | `docs/PRODUCTION_ROLLBACK.md` covers feature-flag and code-level rollback for every subsystem; this pass's own changes are additive with a documented, narrow rollback path. |

## Roll-up

| Status | Count |
|---|---|
| PASS | 18 |
| FAIL | 1 (#23, HTTPS redirect) |
| BLOCKED | 7 (#7, #8, #19, #20*, #21, #22, #24) |
| UNKNOWN | 2 (#27, #28) |
| N/A | 1 (#14) |

\* #20 (backup schedule) is BLOCKED-in-name but functionally closer to a
known gap than a mere verification limit — called out explicitly above,
not averaged away into the BLOCKED count's more benign framing.

## What this table does and doesn't claim

18 of 29 areas are genuinely, verifiably PASS from this environment's own
testing — not aspirational, not "should work." One area (#23, HTTPS
redirect) is a confirmed FAIL requiring a Cloudflare dashboard change.
Seven areas are BLOCKED specifically because they require production
access, a physical device, or real money this environment does not have
— each has an exact unblocking command in `docs/LAUNCH_BLOCKERS.md`, not
a shrug. Two areas are UNKNOWN because they are legal/policy questions no
amount of further engineering resolves. This table does not say "ready
to launch" — see `docs/FINAL_LAUNCH_ACCEPTANCE.md`'s own closing
decision section below for that call.

## Decision

See the GO/NO-GO block at the end of the final report delivered
alongside this document. Summary: **NOT YET GO** — one confirmed FAIL
(#23) and the backup-schedule gap (#20) are both real, fixable-by-a-human
blockers with no code-level workaround, and two UNKNOWN legal/policy
areas (#27, #28) must not be waved through regardless of how clean the
engineering evidence is elsewhere.
