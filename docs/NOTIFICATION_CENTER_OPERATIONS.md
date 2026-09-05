# Notification Center — Operations

## Deployment

The Notification Center adds one migration and needs **two** containers
recreated — the campaign worker runs inside the bot process, not the
admin one, so a deploy that only recreates `admin` ships the API and UI
with no worker to actually act on them.

1. `alembic upgrade head` — confirm revision `72cd4cae946c` (`notification
   center schema`) is applied on top of `b31c5f70f957`.
2. Recreate `admin` (new `/notifications/*` routes, RBAC permissions,
   the console's new nav item) **and** `bot` (`services/bot/campaign_
   worker.py`'s `run_forever()`, started alongside the existing
   `notification_relay.run_forever()` in `_on_startup()`).
3. `GET https://admin.arada.fun/notifications/overview` (with a real
   superadmin/ops bearer token) → `200` confirms the admin side is live.
4. Confirm the worker is actually running: create one real draft
   campaign targeting only your own `user_ids` (see the admin guide's
   "high-risk" note — never a broad audience for a smoke test), send it,
   and watch its status move `queued → sending → completed` within
   roughly `POLL_INTERVAL_SECONDS` (10s) of sending. If it stays `queued`
   indefinitely, the `bot` container's `campaign_task` isn't running —
   check its logs for `campaign_worker_tick_failed`.

Nothing about this feature touches Bingo rounds, the ledger, or any
payment rail — a deploy of this feature carries none of the money-path
regression risk a payments change would.

## Monitoring

`notification_campaign_deliveries_total{outcome}` (Counter, exposed on
the `bot` service's existing `/metrics` endpoint alongside every other
metric that process already emits — `packages/core/metrics.py`) — one
increment per delivery the relay resolves to a terminal outcome:
`delivered`, `blocked` (recipient has blocked the bot — expected at some
background rate, not itself an incident), `gave_up` (Telegram
rate-limited the send until retries were exhausted), `failed` (anything
else). A sudden shift from `delivered` toward `failed`/`gave_up` across
a campaign is the signal worth alerting on; this repository does not
ship a Prometheus alert rule for it yet (unlike the Telebirr rail's
`deploy/prometheus/alerts.yml` entries) — add one against this metric
before running a campaign at real scale if paging matters to you.

A delivery stuck at `processing` (a `bot` crash landing between marking
it `processing` and the message reaching Redis) self-heals automatically
now — `services/bot/campaign_worker.py::_reclaim_stuck_deliveries()`
resets anything idle past `RECLAIM_STUCK_AFTER_SECONDS` (15 minutes) back
to `pending` every tick, and `notification_relay.py`'s own idempotency
check (re-verifying a delivery's live status immediately before ever
calling `notifier.send()`) is what guarantees this can never produce a
real duplicate Telegram message, even in the rare case where the
original send had actually gone through. No manual intervention needed;
see `docs/NOTIFICATION_CENTER_ARCHITECTURE.md`'s "Crash safety" section
for the full mechanism. `notification_delivery_reclaimed_from_stuck_
processing` (a structured log line, `delivery_ids` field) fires whenever
this actually happens — worth watching for a *sustained* rate of these
(an occasional one is expected background noise; a steady stream
suggests the `bot` container is crash-looping, not that this mechanism
itself is broken).

## Troubleshooting

**A campaign stays `draft` forever.** Nothing is wrong — a draft has no
worker action associated with it. Nothing happens until Send/Schedule.

**A campaign stays `queued`/`scheduled` past its time.** The `bot`
container's campaign worker isn't running or has crashed its task loop.
Check `bot` container logs for `campaign_worker_tick_failed` (logged with
the exception, then the loop continues on the next tick — one bad tick
doesn't kill the worker). Confirm `campaign_task` was actually created in
`_on_startup()` (`services/bot/app.py`) by checking the process is the
current build, not a stale image predating this feature.

**A campaign stays `sending` with a nonzero delivered/failed count but
never reaches `completed`.** A `processing` delivery reclaims itself
within `RECLAIM_STUCK_AFTER_SECONDS` (15 minutes) if it's genuinely
stuck, so this is usually just still-`pending` rows the worker hasn't
caught up to yet (`DISPATCH_BATCH_SIZE` is 200 per campaign per tick; a
campaign with tens of thousands of recipients takes multiple ticks by
design, not a bug). If it's been well over 15 minutes with no progress
at all, check the `bot` container is actually running (see the
`queued`/`scheduled` case above).

**A specific recipient never got their message.** Look up their delivery
row's `status`/`failure_reason` on the campaign's detail page. `blocked`
means they've blocked the bot — nothing to fix, not every player can be
reached this way. `gave_up` means sustained Telegram rate-limiting; if
this is common across many recipients in one campaign, the campaign was
larger than the shared `Notifier`'s pace can clear comfortably within its
own retry budget — no per-campaign throttle exists beyond the shared,
global one every other bot message already goes through.

**A player claims they got the same announcement twice.** See the
architecture doc's crash-safety section — this is the accepted, narrow
failure mode of the current design (chosen deliberately over the
alternative, which could silently drop a message instead). It requires
an actual `bot` process crash landing in a specific few-millisecond
window per affected delivery; it is not expected under normal operation.

## Rollback

There is no feature flag for this system (unlike `payment_provider_
availability` for payment rails) — it doesn't need one, since a campaign
never sends unless an admin explicitly clicks Send/Schedule. To stop all
Notification Center activity immediately: cancel any `scheduled`/`queued`
campaigns from the admin console (each cancellable individually — there
is no global kill switch, since each cancel is itself an audited action
with a specific actor), and/or stop the `bot` container's campaign
worker by rolling that container back to a pre-feature image (this also
stops the existing transactional-notification relay running in the same
process, so only do this if you need to stop *all* bot-originated
messages, not just campaigns). Rolling back the migration
(`alembic downgrade -1`) is safe only after confirming no campaign is
`sending` (an in-flight campaign's delivery rows would be dropped along
with the tables).

## Regression scope

This feature adds new tables, a new worker task, and new admin routes;
it does not modify `round_engine.py`, `ledger.py`, any payment rail, or
any existing bot handler's own message-sending path outside `Notifier`'s
internal outcome-reporting change (`services/bot/notifier.py` —
`OutboundMessage.done` now resolves to a `str` outcome instead of `None`,
verified against every existing caller: no test in `tests/unit/
test_notifier.py` or `tests/integration/test_notification_relay.py`
asserted on the old `None` value, so this is backward compatible, not a
breaking change to an existing contract). A full regression run
(`pytest tests/`, excluding load/e2e/chaos per this repo's own default
markers) shows 1074 passed, 2 failed — both failures
(`tests/integration/test_worker.py::test_run_active_rooms_is_safe_to_
call_repeatedly` and `test_run_active_rooms_recovers_a_room_that_dies_
mid_session`) reproduce identically on the original commit with none of
this feature's changes present (confirmed directly via `git stash`),
and are unrelated pre-existing Redis-connection-pool flakes in the
engine worker's own test suite, not a regression this feature
introduced.
