# Agent Dashboard Guide (Agent Portal)

A real, separate application from the admin console — because a Payment
Agent has no `admin_users` identity at all (see
`docs/ADMIN_DASHBOARD_GUIDE.md`'s own "Payment Agents are not admin
users"), a real login had to be built rather than reused. It's still the
same backend service (`services/payments`), the same `payment_evidence`
table, and the same ingestion pipeline every other channel uses — not a
second payment system.

## Access

`https://agent.arada.fun` — live. No username or password exists to
give out.

## How an agent logs in

1. An agent who is already `is_active` in `payment_agents` sends
   `/portal` to the private Telegram bot.
2. The bot (`services/bot/handlers.py`'s `on_agent_portal_command`)
   replies with a one-time link, valid for 5 minutes and usable exactly
   once: `https://agent.arada.fun/login?token=<random>`.
3. Opening it exchanges the token for a normal session (the identical
   opaque-random-token-in-Redis pattern the admin console's own sessions
   use, not a second scheme) and shows the dashboard.

There is no password anywhere in this flow — Telegram is the identity an
agent already provably has, reused rather than duplicated. See
`services/payments/agent_auth.py`'s own module docstring for the full
reasoning.

## What the portal shows

Exactly one screen: **recent submissions**, scoped to *this* agent's own
Telegram-forwarded messages only —

| Column | Source |
|---|---|
| Reference | `payment_evidence.external_reference` |
| Amount | `payment_evidence.amount` |
| Status | `available` / `redeemed` / `rejected` / `blocked` / `disputed` / `expired`, shown as a colored badge |
| Reason | `payment_evidence.reject_reason`, when set |
| Received | `payment_evidence.received_at` |

The query (`GET /agent-portal/submissions`) filters on
`source = 'telegram_agent' AND source_ref = <this agent's own
telegram_user_id>` — verified directly (real HTTP requests, two real
agents, real rows) that agent A's session can never see agent B's
submissions, and never sees a MacroDroid-sourced row at all (those aren't
tied to any agent identity in the first place).

## What the portal deliberately never shows

No raw SMS text, no payer name or phone, no recipient name or phone — an
agent sees only their own submission's parsed outcome, never another
person's private information. Verified directly: every field in a real
API response was checked against this exact list. This is a narrower
view than even the admin console's own `support`/`ops` roles get (they
can see that evidence exists at all, `payments:view`; only
`finance`/`superadmin` can see raw SMS text at all, `payments:
view_raw_evidence`) — an agent's own portal is narrower still, by design.

An agent also cannot: add or deactivate other agents, configure the
recognized recipient, toggle any payment rail, or see any other player's
or agent's data. All of that stays exactly where it already was — the
admin console, superadmin-gated.

## Session lifetime and revocation

Sessions last 8 hours, matching the admin console's own session TTL.
Deactivating an agent (`payment_agents.is_active = false`, done through
the admin console's own Payment Agents screen) invalidates their session
on its very next request, not merely after the TTL expires — the exact
same live re-check `services/admin/auth.py::resolve_session` already
does for admin sessions, verified directly for the agent case with a real
deactivate-then-request test.
