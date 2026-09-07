# RBAC Matrix

The complete, real permission set from `services/admin/rbac.py` — not a
sample. 34 permissions (re-verified by direct introspection of
`PERMISSIONS.keys()` during the Telegram Command Center pass — was 26 at
this doc's original writing; `rooms:emergency_stop`, `bot_content:manage`,
the 4 `bonuses:*` permissions, and the 3 `telegram:*` permissions below
were each added by a later pass and are included here now), 4 roles
(`support`, `finance`, `ops`, `superadmin`), additive only (no role loses
what a "lower" role has; no hidden god-mode bypass exists separate from
this table). Originally verified by direct static analysis of all admin
routes: only `/auth/login`, `/healthz`, `/metrics` have no permission
dependency at all — the only 3 that should be public. Every other route
requires one of the permissions below.

**ACTUAL ACCESS** reflects the code as written (`PERMISSIONS` dict, the
sole source of truth `require()` reads from — no per-route override
exists anywhere). **AUDIT** is YES for every mutation (every
non-`:view` permission below) — confirmed by grep: every admin write
path calls `audit.record()`, and `admin_audit_log` is append-only
(DB-trigger-enforced, no `UPDATE`/`DELETE` grant exists on that table).

| Permission | Support | Finance | Ops | Superadmin | What it gates | Audit |
|---|---|---|---|---|---|---|
| `dashboard:view` | ✅ | ✅ | ✅ | ✅ | Home dashboard | N/A (read) |
| `users:view` | ✅ | ✅ | ✅ | ✅ | User list/detail | N/A (read) |
| `users:adjust_balance` | ❌ | ✅ | ❌ | ✅ | Manual wallet credit/debit | YES |
| `users:suspend` | ❌ | ✅ | ✅ | ✅ | Account suspension | YES |
| `users:verify_kyc` | ❌ | ✅ | ❌ | ✅ | KYC level (gates withdrawal size) | YES |
| `rounds:view` | ✅ | ✅ | ✅ | ✅ | Round history/detail | N/A (read) |
| `rounds:void` | ❌ | ❌ | ✅ | ✅ | Force-void a round | YES |
| `rooms:view` | ✅ | ✅ | ✅ | ✅ | Room list/detail | N/A (read) |
| `rooms:manage` | ❌ | ❌ | ✅ | ✅ | Room config (`PATCH /rooms/{id}`) | YES |
| `rooms:emergency_stop` | ❌ | ❌ | ❌ | ✅ | Immediately halt an active, money-bearing round platform-wide for one room and deactivate it — narrower than `rooms:manage`/`rounds:void` on purpose, since neither alone covers this exact blast radius | YES |
| `reports:view` | ❌ | ✅ | ❌ | ✅ | Financial reports | N/A (read) |
| `audit:view` | ❌ | ❌ | ❌ | ✅ | The audit log itself | N/A (read) |
| `payments:view` | ✅ | ✅ | ✅ | ✅ | Payment list/status | N/A (read) |
| `payments:approve` | ❌ | ✅ | ❌ | ✅ | Manual deposit/withdrawal approval | YES |
| `payments:view_raw_evidence` | ❌ | ✅ | ❌ | ✅ | Raw Telebirr SMS text (deliberately narrower than `payments:view` — least-privilege for payer PII) | N/A (read, but access itself is worth auditing — see note below) |
| `payments:configure` | ❌ | ❌ | ❌ | ✅ | Provider Availability toggle — the single highest-leverage lever short of `admin_users:manage` | YES |
| `risk:view` | ❌ | ✅ | ✅ | ✅ | Risk/fraud investigation screen | N/A (read) |
| `notifications:view` | ❌ | ❌ | ✅ | ✅ | Campaign list/detail | N/A (read) |
| `notifications:create` | ❌ | ❌ | ✅ | ✅ | Draft a campaign | YES |
| `notifications:send` | ❌ | ❌ | ❌ | ✅ | Actually broadcast | YES |
| `notifications:schedule` | ❌ | ❌ | ❌ | ✅ | Schedule a future send | YES |
| `notifications:cancel` | ❌ | ❌ | ❌ | ✅ | Cancel a queued/scheduled send | YES |
| `notifications:templates_manage` | ❌ | ❌ | ✅ | ✅ | Reusable message templates | YES |
| `notifications:view_analytics` | ❌ | ❌ | ✅ | ✅ | Delivery/outcome analytics | N/A (read) |
| `notifications:view_delivery_details` | ❌ | ❌ | ✅ | ✅ | Per-recipient delivery status | N/A (read) |
| `admin_users:manage` | ❌ | ❌ | ❌ | ✅ | Create/deactivate/reset-password/role-change admin accounts — decides who holds every other permission in this table | YES |
| `bot_content:manage` | ❌ | ❌ | ✅ | ✅ | Player-facing bot text overrides | YES |
| `bonuses:view` | ✅ | ✅ | ✅ | ✅ | Bonus/referral row status | N/A (read) |
| `bonuses:manage_rules` | ❌ | ❌ | ✅ | ✅ | Bonus rule configuration | YES |
| `bonuses:grant` | ❌ | ✅ | ❌ | ✅ | Manual ad-hoc bonus grant — same shape as `users:adjust_balance` | YES |
| `bonuses:view_fraud_signals` | ❌ | ✅ | ✅ | ✅ | Referral fraud-clustering signals | N/A (read) |
| `telegram:view_health` | ✅ | ✅ | ✅ | ✅ | Live Telegram webhook health (`getWebhookInfo`) — same breadth as `dashboard:view`; a read-only, side-effect-free check every role benefits from | N/A (read) |
| `telegram:commands_view` | ✅ | ✅ | ✅ | ✅ | Command registry + live per-command metrics | N/A (read) |
| `telegram:commands_manage` | ❌ | ❌ | ✅ | ✅ | Enable/disable a command, edit its description/category/cooldown/rate limit, preview content, send a test message | YES |

## Separation-of-duties properties this table actually enforces

- **No role can grant itself more access.** Only `admin_users:manage`
  (superadmin-only) can create or modify an admin account's role — no
  lower role has any path to escalate itself or anyone else.
- **No role bypasses the money-movement approval gate on its own.**
  `payments:approve`/`users:adjust_balance`/`bonuses:grant` are all
  finance+superadmin only; `ops` — despite broad operational access
  (rooms, notifications, bonus rules, bot content) — cannot move money
  directly anywhere in this table.
- **The two highest-leverage single actions in the system**
  (`payments:configure` — redirect where all deposits settle;
  `notifications:send` — broadcast to every targeted player at once) are
  both superadmin-only, on the explicit reasoning (in the code's own
  comments) that these are the single actions where a compromised
  account could do system-wide damage in one call, not bounded to one
  request the way `payments:approve` is.
- **`support` is read-mostly by design** — the only non-`:view`
  permission it holds anywhere in this table is none; every row where
  `support` is checked is a read permission. A compromised support
  account cannot mutate anything through this RBAC table.

## One thing this table does not by itself guarantee

`payments:view_raw_evidence` grants *read* access to payer PII (a raw
SMS's phone-number fragment) — this is appropriately narrow (finance/
superadmin only, not the broader `payments:view` audience), but reading
it is not currently a distinctly-audited event the way every *mutation*
in this table is (there's no `admin_audit_log` row generated merely for
*viewing* a raw evidence record, only for *actions*). Whether read-access
to this specific PII should itself be logged is a real, minor
data-governance question worth a decision, not treated here as either a
gap or a non-issue — flagged for awareness.

## Verification

Every boundary in this table has been exercised over real HTTP in this
project's test suite (403 for a role that shouldn't have access, 200 for
one that should) across this and prior sessions — not merely read from
the `PERMISSIONS` dict and assumed enforced. The route-level audit this
pass (exactly 3 of 79 routes with no permission dependency, all
correctly public) is the structural confirmation that this table is the
*only* gate — no route bypasses it.
