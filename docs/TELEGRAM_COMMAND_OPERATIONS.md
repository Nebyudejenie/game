# Telegram Command Operations

The Telegram Command Center: a single, authoritative, admin-visible
registry of every real bot command handler, safe admin customization,
graceful enable/disable, content preview, a safe test-send primitive,
and a full audit trail. Built on top of the Telegram command latency
diagnosis pass's own instrumentation (`docs/TELEGRAM_PERFORMANCE.md`) --
nothing here duplicates that work; the registry and the metrics pipeline
are two separate, complementary layers over the same 18 real handlers
(`docs/BOT_COMMAND_CATALOG.md`).

## 1. Why a registry, and what it is not

Before this feature, disabling a command, editing its description, or
changing an operational parameter required a code change and a
deployment -- a real gap for a live, money-adjacent product where a
single misbehaving command (say, a bug found in `/withdraw` at 2am)
previously had no faster mitigation than a hotfix deploy.

**The database stores configuration, never code.** `bot_commands`
(`migrations/versions/232a259a3baa_bot_command_registry.py`) has one row
per real handler function in `services/bot/handlers.py`, identified by
that function's own `handler_name` (e.g. `cmd_balance`) -- never an
invented identifier that could drift from the actual code. Every field
is descriptive/behavioral configuration (enabled, visible, sort order,
cooldown, rate limit, display text, content key) -- there is no field
that stores or evaluates arbitrary code, SQL, or templates beyond
`str.format()`-style `{placeholder}` substitution (the same mechanism
`services/bot/i18n.py::t()` already used before this feature existed).

## 2. Architecture

```
bot_commands (Postgres)
      │
      │  polled every 30s (services/bot/command_registry.py::run_forever),
      │  same pattern as bot_content_sync.py
      ▼
in-process cache (services/bot/command_registry.py)
      │
      │  read by two aiogram inner middlewares, registered in this order
      │  (registration order is execution order -- confirmed against
      │  aiogram's own installed MiddlewareManager, not assumed):
      ▼
1. command_gate_middleware   -- blocks a disabled command, replies with
                                 the configured "temporarily unavailable"
                                 message, increments telegram_command_
                                 blocked_total, and returns *before*
                                 perf_middleware starts timing (a
                                 deliberately disabled command should
                                 never pollute that command's own
                                 latency/success/error stats)
2. perf.perf_middleware      -- unchanged from the latency diagnosis pass
      │
      ▼
the real handler function runs (or doesn't, if blocked above)
```

A handler with **no row** in `bot_commands` (should never happen given
the seed migration, but true for any hypothetical future handler added
before its own registry row exists) is treated as **enabled and
visible** -- the registry is additive opt-out, not opt-in, so a new
command is never silently disabled just because nobody has registered it
in the admin UI yet.

## 3. Admin API

| Endpoint | RBAC | Purpose |
|---|---|---|
| `GET /telegram/commands` | `telegram:commands_view` (support/finance/ops/superadmin) | Full registry + live metrics per handler (see §4) |
| `PATCH /telegram/commands/{handler_name}` | `telegram:commands_manage` (ops/superadmin) | Edit `display_name`/`description`/`category`/`enabled`/`visible`/`sort_order`/`cooldown_seconds`/`rate_limit_per_minute`/`content_key`/`analytics_key`. Refuses to touch `enabled`/`visible` on a structural (`admin_managed=false`) row with a 422, not a raw exception. |
| `GET /telegram/commands/{handler_name}/preview?language=am` | `telegram:commands_view` | Renders the command's current content (default or admin override) with every required placeholder replaced by a clearly-marked `[name]` sample -- never a fabricated real value. |
| `POST /telegram/commands/{handler_name}/send-test` | `telegram:commands_manage` | Sends that same preview text to exactly one admin-supplied `target_telegram_id`. There is no default/broadcast audience -- the request always names one recipient, so there is no code path from here that can reach more than one person. |

Every `PATCH` and every `send-test` call writes a real row to
`admin_audit_log` (`services/admin/audit.py`, the same append-only,
UPDATE/DELETE-refusing table every other admin action already uses) --
admin, action, target (`bot_command`, the handler name), before/after
(only the changed fields), reason, IP address, timestamp.

## 4. Live metrics without a second monitoring system

The admin process and the bot process are separate services with no
shared in-process Prometheus registry. `services/admin/
bot_metrics_client.py` fetches the bot's own already-existing `/metrics`
endpoint over HTTP (`Settings.bot_metrics_url`, empty by default) and
parses it with `prometheus_client`'s own official parser -- reusing
exactly the `telegram_commands_total`/`_success_total`/`_error_total`/
`_blocked_total`/`_latency_seconds` metrics the latency diagnosis pass
already built and tested, adding no new instrumentation. Percentiles are
computed with the same linear-interpolation-within-bucket algorithm
PromQL's `histogram_quantile()` uses.

**A command with zero real traffic shows `NO DATA`, never `0`** -- both
in the API response (`metrics: null`) and the admin UI. `bot_metrics_url`
unset (the default, since no real bot process is reachable from a given
admin deployment's own network path without configuration) means every
row shows `NO DATA`, which is the honest state for an environment where
the two services can't actually reach each other yet, not an error.

## 5. Content preview and safe test-send

Both reuse the bot's existing i18n system (`services/bot/i18n.py`,
`bot_i18n_overrides` -- the same table and fallback chain the pre-existing
Bot Content admin screen already manages) rather than a second content
store. A command's `content_key` (e.g. `cmd_balance` → `balance.summary`)
names the i18n key its reply is built from; preview resolves the current
value (admin override if one exists, else the shipped default) and
substitutes every `{placeholder}` the template requires with a bracketed
sample (`[cash]`, `[bonus]`, ...) so an admin can read the exact copy and
formatting a player would see without this system fabricating a real
balance, amount, or link on their behalf.

"Send test" reuses `packages/core/notifications.py`'s existing
`NOTIFICATIONS_STREAM` -- the same cross-process pipeline every other
admin-originated Telegram message (deposit confirmations, Notification
Center campaigns) already goes through, so a test message gets the same
rate pace, 429 backoff, and delivery-outcome handling as a real one, with
no second delivery mechanism to keep in sync.

Three of the 18 handlers have no `content_key` set (`cmd_deposit`,
`cmd_withdraw`, `on_agent_sms`) -- each replies with one of several
outcome-specific strings depending on what actually happened (rate
limited, below minimum, provider error, ...), so there is no single
"the" reply to preview or test-send. Attempting either against one of
these returns a clear 404 (`MissingContentKey`), not a confusing empty
render.

## 6. Rate limit/cooldown enforcement (Phase 3)

Both `cooldown_seconds` and `rate_limit_per_minute` are now genuinely
enforced, not just stored -- `services/bot/command_registry.py::
command_gate_middleware` checks both (right after the enable/disable
check, before the real handler ever runs) using the exact same Redis
token-bucket primitive every other rate limit in this codebase already
uses (`packages/core/rate_limit.py`, also backing the deposit cap, the
gateway's per-connection limits, and the admin login brute-force
throttle) -- extended additively with a new `allow_with_retry_after()`
function (the existing `allow()` is now implemented in terms of it, with
zero behavior change for any of its pre-existing callers) so a denied
request gets a real, computed "try again in N seconds", not a bare "too
many requests" or a guess.

A cooldown is a capacity-1 bucket refilling once every `cooldown_seconds`
(a second attempt before that time elapses is denied outright); a rate
limit is a capacity-N bucket refilling continuously at N/60 per second (a
smoother, burst-tolerant version of "N per minute", not a hard
reset-every-60-seconds window). Both are keyed per `(handler_name,
telegram_id)`, so one player hitting their own limit never affects
another, and one command's limit never affects a different command for
the same player.

Blocking happens entirely inside the gate middleware, before the real
handler is ever called -- this is what makes rate limiting inherently
safe against duplicate financial effects (Section 3's own requirement):
a denied request never reaches `cmd_deposit`/`cmd_withdraw`/etc. at all,
so there is no financial code path that could have run twice.

Observability: `telegram_command_rate_limited_total{handler, limit_type}`
(a new Counter, `limit_type` is `"cooldown"` or `"rate_limit"`), visible
on both the bot's `/metrics` and the admin Commands screen (as "Rate-
limited" hits) and the Grafana dashboard's own new panel. Admin
configuration is validated server-side: `cooldown_seconds` must be a
non-negative integer capped at 3600 (1 hour); `rate_limit_per_minute`
must be null (explicitly unlimited) or a positive integer capped at 1000
-- both reject with a clear 422 rather than a raw DB constraint failure
or silently-accepted nonsense.

## 7. Analytics key wiring (Phase 3)

An admin-set `analytics_key` now relabels every metric this codebase
attributes to a command -- `telegram_commands_total`, `_success_total`,
`_error_total`, `_latency_seconds` (`services/bot/perf.py`), the DB/Redis
timing histograms (same file, via the same resolved label feeding the
shared `_current_command` contextvar), and both of command_registry.py's
own counters (`telegram_command_blocked_total`,
`telegram_command_rate_limited_total`). Resolution happens in exactly
one place (`command_registry.analytics_label()`): the real handler
function name unless an override is set, so every existing Grafana
panel/alert built against a real function name (the overwhelming
majority -- no command has an analytics_key set by default) is
completely unaffected. `services/admin/command_registry_queries.py::
list_commands_admin()` resolves the identical way when joining live
`/metrics` data back to each registry row, so a command with a custom
analytics_key shows its real numbers in the admin UI instead of a
false NO DATA.

The Redis rate-limit/cooldown bucket keys deliberately stay on the raw
`handler_name`, never the analytics label -- an admin renaming a
command's analytics identity must never reset or fragment a player's
already-in-progress cooldown/rate-limit state.

## 8. A visible/sort_order-driven dynamic `/help` listing

Still not built. The registry has the fields a generated help menu would
need (`visible`, `sort_order`, `category`, `display_name`); no handler
currently builds one from them. Noted here as a real, bounded, still-open
deferral -- not attempted this phase either, since it's a net-new player-
facing feature rather than finishing an already-started one.
