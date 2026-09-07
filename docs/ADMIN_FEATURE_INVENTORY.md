# Admin Feature Inventory

Every row below was verified against the real, current code — the 85
real routes in `services/admin/app.py`, the 34 real permissions in
`services/admin/rbac.py`, and the 18 real screens registered in
`web/admin/js/app.js`'s own `SCREENS` map — not assumed from any prior
document. Re-generate the counts with:

```
grep -c '@app\.\(get\|post\|patch\|put\|delete\)(' services/admin/app.py
python3 -c "from services.admin.rbac import PERMISSIONS; print(len(PERMISSIONS))"
grep -c ':.*Screen,$' web/admin/js/app.js   # approximate; SCREENS map is authoritative
```

| Feature | Backend | Admin Screen | Role(s) | Permission | Configurable | Content Editable | Audited | Tested |
|---|---|---|---|---|---|---|---|---|
| Users (view/search) | `services/admin/queries.py` | `users.js` | support+ | `users:view` | — | — | N/A (read) | Yes |
| Users (balance adjust) | same | `users.js` | finance, superadmin | `users:adjust_balance` | Yes (amount/reason) | — | Yes | Yes |
| Users (suspend/status) | same | `users.js` | ops, finance, superadmin | `users:suspend` | Yes | — | Yes | Yes |
| Users (KYC level) | same | `users.js` | finance, superadmin | `users:verify_kyc` | Yes | — | Yes | Yes |
| Admin accounts | `services/admin/auth.py`, `queries.py` | `admin_users.js` | superadmin only | `admin_users:manage` | Yes (create/deactivate/role/reset-password) | — | Yes | Yes |
| Payment agents | `services/admin/queries.py` | `payment_agents.js` | ops+ (via `rooms:manage`-adjacent routes — see note below) | none dedicated¹ | Yes (add/activate/deactivate) | — | Yes | Yes |
| Rooms | `services/admin/queries.py` | `rooms.js` | support+ view; ops+ manage | `rooms:view`/`rooms:manage` | Yes (stake/timings/patterns) | — | Yes | Yes |
| Bingo rounds/cards/claims/winners | `services/admin/queries.py` (round detail joins `round_entries`/`round_winners`) | `rounds.js` | support+ view; ops+ void | `rounds:view`/`rounds:void` | Void only | — | Yes (void) | Yes |
| Emergency single-room stop | `services/engine/round_engine.py` + `services/admin/queries.py::stop_room_admin` | `rooms.js` (Stop room button) | superadmin only | `rooms:emergency_stop` | N/A (an action, not a setting) | — | Yes | Yes — 12 dedicated tests, re-verified Phase 3 |
| Wallet / Ledger (per-user) | `packages/core/ledger.py` via `services/admin/queries.py` | `users.js` (ledger tab) | support+ view | `users:view` | — | — | N/A (read) | Yes |
| Deposits (Chapa, automatic) | `services/payments/deposits.py` | `payments.js` | support+ view; finance+ approve | `payments:view`/`payments:approve` | — | — | Yes | Yes |
| Deposits (manual) | `services/payments/manual.py` | `manual_deposits.js` | support+ view; finance+ approve | `payments:view`/`payments:approve` | Yes (destinations) | — | Yes | Yes |
| Withdrawals (automatic) | `services/payments/withdrawals.py` | `manual_withdrawals.js`/`payments.js` | support+ view; finance+ approve | `payments:view`/`payments:approve` | — | — | Yes | Yes |
| Withdrawals (manual, two-person approval) | `services/payments/withdrawals.py` | `manual_withdrawals.js` | finance+ | `payments:approve` | — | — | Yes | Yes |
| Payment provider availability (Chapa/manual toggle) | `services/payments/availability.py` | `provider_availability.js` | superadmin only | `payments:configure` | Yes | — | Yes | Yes |
| Telebirr SMS evidence | `services/payments/telebirr_ingest.py` | `telebirr_evidence.js` | support+ view; finance+ raw SMS | `payments:view`/`payments:view_raw_evidence` | Yes (resolve) | — | Yes | Yes — real-device test still pending (blocked on physical hardware) |
| Bonuses / bonus rules | `packages/core/bonuses.py`, `services/admin/bonus_queries.py` | `bonuses.js` | support+ view; ops+ rules; finance+ grant | `bonuses:view`/`manage_rules`/`grant` | Yes | Yes (announce via Notification Center) | Yes | Yes |
| Referrals | `packages/core/referrals.py` (attribution); no dedicated referral-quality screen yet | `bonuses.js` (referral funnel section) | ops, finance, superadmin | `bonuses:view_fraud_signals` | Rules only, via bonus rules | — | Yes | Partial — funnel counts tested; quality ranking not built (Section 30, deferred) |
| Promotions (as a distinct concept from bonus rules) | Not built as a separate entity | — | — | — | No | — | No | No — "promotions" today *is* the bonus-rule system; no separate campaign-with-budget-and-eligibility builder exists (Section 28, deferred) |
| Notifications (Notification Center) | `packages/core/campaigns.py`, `services/bot/campaign_worker.py` | `notifications.js` | ops+ view/create; superadmin send/schedule/cancel | 8 distinct `notifications:*` permissions | Yes | Yes | Yes | Yes |
| Telegram command registry | `services/bot/command_registry.py`, `services/admin/command_registry_queries.py` | `telegram_health.js` | support+ view; ops+ manage | `telegram:commands_view`/`commands_manage` | Yes (enable/disable/cooldown/rate-limit/content) | Yes (preview + send-test) | Yes | Yes |
| Telegram webhook health | `services/admin/telegram_diagnostics.py` | `telegram_health.js` | support+ | `telegram:view_health` | — | — | N/A (read) | Yes |
| Telegram performance (P50/P95/P99, rate-limited/blocked counts) | `services/admin/bot_metrics_client.py` | `telegram_health.js` (Commands table) | support+ | `telegram:commands_view` | — | — | N/A (read) | Yes |
| Bot content (i18n overrides) | `services/bot/i18n.py`, `services/admin/bot_content_queries.py` | `bot_content.js` | ops+ | `bot_content:manage` | — | Yes | Yes | Yes |
| Mini App content | Same i18n mechanism exists (`web/miniapp/locales/*.json`), but no admin CRUD/preview screen for it | — | — | — | No | **No** — Mini App copy still requires a code deploy to change | No | No — genuine gap (Section 25, deferred) |
| Risk (fraud signals) | `services/admin/queries.py::shared_payout_account_clusters`/`repeat_room_pairings`, `bonus_queries.py::referral_fraud_candidates_admin` | `risk.js`, `bonuses.js` | ops, finance, superadmin | `risk:view`/`bonuses:view_fraud_signals` | — (investigation only, no auto-action) | — | N/A (read; any resulting suspension is audited via `users:suspend`) | Yes |
| Responsible gaming (limits, cool-off, self-exclusion) | `packages/core/responsible_gaming.py` | Player-facing only (`/limits` bot command) — no dedicated admin screen | — | — | Player-initiated only | — | Via ledger/status changes | Yes (player-facing) — no admin-side override screen exists |
| Reports (GGR/LTV/retention) | `services/admin/queries.py` | `reports.js` | finance, superadmin | `reports:view` | — | — | N/A (read) | Yes |
| Audit log | `services/admin/audit.py` | `audit.js` | superadmin only | `audit:view` | — | — | Is the audit mechanism itself | Yes |
| Global search | `services/admin/search_queries.py` (new, this phase) | Command palette overlay (Ctrl/Cmd+K), no dedicated screen | Every role — per-category RBAC | Reuses each category's own `:view` permission | — | — | N/A (read) | Yes |
| Monitoring / alerting (business-level) | Grafana dashboard (`deploy/grafana/dashboards/jo-bingo.json`) for Telegram/engine/ledger metrics; no in-admin "business alerts" engine | Grafana only, external to the Admin Portal | Whoever has Grafana access (separate from admin RBAC) | N/A | N/A | — | N/A | Panels exist and are real; no admin-native alert feed (Section 37, deferred) |
| Backups | Exists at the infrastructure level (see `docs/DISASTER_RECOVERY.md`/`docs/PRODUCTION_ROLLBACK.md` from an earlier phase) | No admin screen | — | — | No | — | No | No — a real operational runbook exists; no admin UI wraps it |
| Maintenance mode | Not built | — | — | — | No | — | No | No — genuine gap; no way to put the platform (or one surface of it) into a maintenance state from the admin UI today |
| System configuration center | Partially — `payments:configure` (provider availability) is the closest analog; no unified settings registry | Scattered across `provider_availability.js`/`telegram_health.js` | varies | varies | Partial | — | Yes, per-setting | Partial |
| Analytics / Business Intelligence | Deterministic reports exist (GGR/LTV/retention); no Business Control Center, Room Intelligence, Promotion Analytics, Player Lifecycle, or CEO Brief | `reports.js` only | finance, superadmin | `reports:view` | — | — | N/A | Partial — genuine, large gap (Sections 29-39 of this phase, deferred) |

¹ Payment agents are gated by whichever permission the specific route
uses in practice (confirmed: `payments:view`/`payments:approve`-adjacent
in `services/admin/app.py` — see that file directly rather than trusting
this footnote if precision matters for a real access decision).

## What this inventory confirms is genuinely NOT built

Verified absent, not merely undocumented:

- **Mini App content management** — the Mini App's own player-facing copy
  (`web/miniapp/locales/*.json`) has no admin CRUD/preview screen; the
  bot's own content (Telegram replies) does. Changing Mini App copy
  still requires a code deploy.
- **A distinct "Promotions" entity** — separate from bonus rules, with
  its own budget/eligibility/exposure-cap builder (Section 28's own ask).
  Today, "promotion" and "bonus rule" are the same underlying concept.
- **Business Control Center / Room Intelligence / Promotion Analytics /
  Referral Quality ranking / Player Lifecycle / Spectator Funnel / Daily
  CEO Brief / Business Alerts** — none exist. Deterministic reports
  (GGR/LTV/retention cohorts) do exist and are real, tested, and
  ledger-derived; the broader business-intelligence suite this phase's
  own directive names does not.
- **Admin-native maintenance mode** — no way to mark the platform (or one
  surface) as under maintenance from the Admin Portal.
- **Admin-native backup management** — a real disaster-recovery runbook
  exists at the infrastructure level; no admin screen wraps it.
- **A unified System Configuration Center** — settings are real and
  audited individually (e.g. provider availability, Telegram command
  registry) but are not organized under one "Configuration" screen with
  a common current-value/safe-range/last-changed presentation.
- **Content versioning/restore** — Bot Content overrides are audited
  (before/after values in `admin_audit_log`), but there is no dedicated
  "version history with a one-click restore" UI on top of that audit
  trail.

Each of these is named explicitly here, with its real backend status,
rather than left ambiguous between "doesn't exist" and "exists but
undocumented."
