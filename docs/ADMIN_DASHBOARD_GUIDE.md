# Admin Dashboard Guide

One console (`services/admin` + `web/admin`), four roles
(support/ops/finance/superadmin), RBAC-gated per screen — not separate
apps for admin and finance. "Admin dashboard" and "Finance dashboard" in
earlier planning documents both refer to sections of this one app,
distinguished by what a given role's login can see and do, not by
separate deployments. See `docs/FINANCE_DASHBOARD_GUIDE.md` for the
finance-specific view, `docs/AGENT_DASHBOARD_GUIDE.md` for the separate
Agent Portal (a genuinely different app, since agents have no admin-role
identity at all), and `docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` for why
`admin.arada.fun` and `finance.arada.fun` both point at this exact same
container rather than two deployments.

Access: `https://admin.arada.fun/console/` and
`https://finance.arada.fun/console/` — live (same login, same console;
the hostname you're told to use just matches your role). Gated by
`ADMIN_IP_ALLOWLIST` if configured, in addition to the password+TOTP
login — verified live, both together. `http://127.0.0.1:8001` on the
production host via an SSH tunnel still works too, unchanged.

## The 17 screens (`web/admin/js/app.js`'s own nav order)

| Screen | Backing endpoint(s) | Visible to |
|---|---|---|
| Dashboard | `GET /dashboard` | all four roles |
| Users | `GET /users`, user detail/search | all four roles |
| Payments | `GET /payments` | all four roles |
| Manual Deposits | `services/admin/queries.py` manual-deposit review | all four roles |
| Manual Withdrawals | manual-withdrawal review/approve | all four roles |
| Payment Destinations | `GET/POST/PATCH /manual-payment-destinations` | all four roles (edit is `payments:configure`, superadmin-only, enforced server-side even though the screen is visible to everyone) |
| Telebirr Evidence | `GET /telebirr-evidence`, raw-SMS view, resolve | all four roles can list; raw SMS view (`GET /telebirr-evidence/{id}/raw-sms`) and resolve are `payments:view_raw_evidence`/`payments:approve` — finance + superadmin only, enforced server-side |
| Payment Agents | `GET/POST/PATCH /payment-agents` | all four roles can view; add/deactivate is `payments:configure`, superadmin-only |
| Provider Availability | `GET /payment-provider-availability`, toggle | all four roles can view; toggling `telebirr_sms`/`chapa`/`manual` on or off is superadmin-only |
| Rounds | round history/detail | all four roles |
| Rooms | room config CRUD | all four roles (mutations are superadmin-gated server-side where they affect money — win_patterns, stake, etc.) |
| Notifications | `services/admin/app.py`'s `/notifications/*` routes — see `docs/NOTIFICATION_CENTER_ADMIN_GUIDE.md` | **ops + superadmin only** (hidden from support/finance in the nav and in `rbac.py`; drafting/viewing/templates/analytics is ops+superadmin, actually sending/scheduling/cancelling a real send is superadmin-only) |
| Bot Content | `GET /bot-content`, `PUT/DELETE /bot-content/{key}/{language}` | **ops + superadmin only** — see below |
| Reports | aggregate financial reporting | **finance + superadmin only** (hidden from support/ops in the nav; a direct API call from support/ops still gets a real `403`, verified) |
| Risk | risk/fraud signals | **ops + finance + superadmin only** |
| Audit Log | `GET /audit-log`, filterable by `admin_id`/`action` | **superadmin only** |
| Admin Users | `GET/POST /admin-users`, `PATCH .../active`, `PATCH .../role`, `POST .../reset-password` | **superadmin only** — see below |

The nav-visibility list (`SCREEN_VIEW_ROLES` in `app.js`) is a courtesy —
its own comment says so directly: "the backend remains the sole real
enforcement — this only ever hides a button faster than a click-then-403
would." Verified directly this session: an unauthenticated request and a
garbage-bearer-token request both against `/dashboard` and against the
superadmin-only `/audit-log` returned `403` from the real server, not a
200 that only the UI happened to hide.

## Payment Agents are not admin users

A "Payment Agent" (someone forwarding Telebirr SMS via the private
Telegram bot) is a row in the `payment_agents` table
(`telegram_user_id`, `is_active`), not an `admin_users` row and not a
role. They never log into this console — they have their own separate
portal instead (`docs/AGENT_DASHBOARD_GUIDE.md`), authenticated through
Telegram rather than a password, since this console's own
username/password/TOTP login has no meaning for an identity that only
ever exists as a Telegram user. An admin (with `payments:configure`,
superadmin-only) manages the agent allowlist itself — who's authorized
at all — through the **Payment Agents** screen above; that's a different
capability from an agent viewing their own submission history, which is
what the separate Agent Portal is for. That screen also shows each
agent's real activity (`submission_count`/`last_submission_at`, joined
from `payment_evidence` where `source = 'telegram_agent'`) — not just
the allowlist, so a superadmin can tell an authorized-but-idle agent
from one actually forwarding SMS.

## Admin Users — who can log into this console at all

Before this screen existed, every admin account (support/finance/ops/
superadmin) was provisioned entirely out-of-band: `services/admin/
auth.py::create_admin_user()` always existed, but nothing in the console
ever called it — a new account meant someone with direct database/script
access running it by hand. The **Admin Users** screen (and the
`admin_users:manage` permission behind it, `rbac.py`'s own comment calls
it "the single highest-leverage lever in the whole system, higher even
than `payments:configure`") replaces that with a real console flow:

- **Create** an account (username, temporary password, role) — the TOTP
  secret and provisioning URI are shown exactly once, in the response to
  that one request, and are never retrievable again afterward (same
  guarantee the script-based path always made, now surfaced in the UI).
- **Activate/deactivate** — `auth.resolve_session()` already re-checks
  `is_active` on every request, so deactivating someone revokes their
  *current* session immediately, not just future logins.
- **Change role** — takes effect on that admin's next login (an
  already-issued session keeps the role it was issued with until it
  expires or they log in again).
- **Reset password** — for a forgotten password; never logs the new
  value anywhere, including the audit trail.

An admin can't deactivate or change the role of their **own** account
through this screen (`CannotModifyOwnAccount`, a `409`) — both would risk
a superadmin locking themselves out mid-session with no one else able to
undo it from inside the console.

## Bot Content — editing what the bot says without a deploy

Every player-facing bot string (`services/bot/i18n.py`'s `t()`) ships as
a file-based default in `services/bot/locales/{am,en,om,ti}.json` — the
main menu button labels in the reply keyboard
(`services/bot/keyboards.py::main_menu_keyboard`, e.g. "🎮 ይጫወቱ" / Play)
are the most visible example, but every one of the ~85 keys is covered
the same way. The **Bot Content** screen lets ops/superadmin override any
of them per language, live, with no code deploy:

- Search/browse by key or category (derived from the key's own prefix —
  `menu.*`, `register.*`, `wallet.*`, etc.).
- Edit shows all four languages side by side; a value still on its
  shipped default reads "shipped default," an edited one reads
  "customized."
- **Reset to default** removes the override row entirely rather than
  copying the default text back in — so a later change to the shipped
  default (a real code deploy) takes effect immediately for anyone who
  never customized that key, instead of being masked by a stale copy.
- A key with `{placeholder}` fields (e.g. `register.success`'s `{name}`)
  shows exactly which ones it needs; saving a value that drops or adds
  one is rejected with a `422` before it can reach a real player and
  crash mid-`.format()` — validated by comparing the submitted value's
  own placeholder set against the shipped default's, not a hardcoded
  allowlist.

**The live-update path, and its real latency**: the admin API writes
straight to Postgres (`bot_i18n_overrides`); the bot process (a
separate, always-running service) polls that table into an in-memory
cache every `bot_content_sync.POLL_INTERVAL_SECONDS` (30s) and reloads it
once more on its own startup before handling its first update. `t()`
itself stays a plain synchronous function with no per-call database
round-trip — every existing call site across the bot codebase (dozens of
them) keeps working unchanged. The practical effect: an edit here reaches
real player conversations within about 30 seconds, not instantly.

## Financial data safety (verified, not assumed)

Every number this console shows is a live read from the same tables the
Bingo engine and ledger already treat as canonical
(`rooms`/`rounds`/`round_winners`/`payments`/`payment_evidence`/ledger
`accounts`/`entries`) — there is no separate admin-side balance
computation anywhere in `services/admin/queries.py`. The console never
writes a balance directly either; every money-moving admin action
(approve a manual deposit/withdrawal, resolve payment evidence) goes
through `packages/core/ledger.post()`, the exact same entry point every
other payment rail uses.
