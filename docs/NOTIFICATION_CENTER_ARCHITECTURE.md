# Notification Center Architecture

Broadcast/targeted messaging to players, built entirely on top of
infrastructure that already existed for a different purpose (the bot's
own transactional notifications — "your withdrawal was approved", "your
round starts now") rather than as a second, parallel delivery system.
Nothing here duplicates `packages/core/ledger.py`, the RBAC layer, or the
bot's outbound pipeline; it extends them.

## Channel scope: Telegram only

There is no email, SMS, or push-notification system anywhere in this
codebase (`grep`-verified, zero matches), and no in-app inbox separate
from a Telegram message. The Notification Center sends Telegram messages
through the bot — that is the one and only channel, reflected directly in
`notification_templates.channel`/`notification_campaigns.channel`'s own
`CHECK (channel IN ('telegram'))` constraint. Any future doc, dashboard
label, or report that implies "email campaign" or "push campaign" is
wrong; this system has never sent either.

## Reused, not duplicated

| Need | Reused from | Why not build a second one |
|---|---|---|
| Rate-limited/429-backed-off outbound Telegram calls | `services/bot/notifier.py`'s `Notifier` | It's the *only* thing allowed to call the Telegram send API (`services/bot/handlers.py`'s own invariant) — a second sender would race it for the same per-chat rate budget |
| Queueing a message for async delivery | `packages/core/notifications.py`'s `bot_notifications` Redis Stream + `services/bot/notification_relay.py` consumer | Already a durable, at-least-once, consumer-group-based queue with redelivery on crash |
| RBAC | `services/admin/rbac.py` | One permission table, one `require()` dependency, no per-feature reimplementation |
| Audit logging | `services/admin/audit.py` | Same append-only, before/after/reason/ip log every other admin mutation uses |
| Admin session auth | `services/admin/auth.py` | Same bearer-token session every other admin route uses |

## What's new, and why nothing existing covered it

- **`packages/core/campaigns.py`** — audience resolution (`users` table
  → a list of user ids, from a small fixed set of recognized filter
  keys, never a client-supplied SQL fragment) and delivery bookkeeping.
  Nothing in this codebase previously needed to answer "which subset of
  players match a filter" server-side.
- **Two ingest paths, one stream** — `notify_user()` (existing, i18n-key
  based: `{key, kwargs}`) and `enqueue_campaign_message()` (new,
  admin-authored raw text: `{raw_text, delivery_id}`). Both land on the
  same `bot_notifications` stream; `notification_relay.py::process_one()`
  branches on whether `raw_text` is present. An admin-typed announcement
  has no i18n key to look up, so it can't reuse the existing shape as-is,
  but it reuses the exact same queue, consumer, and `Notifier` call.
- **`services/bot/campaign_worker.py`** — the piece that decides *when*
  a campaign moves and *who* it goes to. Nothing existing does
  scheduling or bulk-fan-out to a resolved audience.

## Schema (migration `72cd4cae946c`)

Three tables: `notification_templates`, `notification_campaigns`,
`notification_deliveries` (one row per recipient per campaign, `UNIQUE
(campaign_id, user_id)`). See the migration file for exact columns/
constraints/indexes.

`notification_campaigns.audience_filter` is a small JSON object
recognized by `packages/core/campaigns.py::_build_where()` — only these
keys turn into SQL, nothing else, and it never accepts a raw filter
string:

`user_ids`, `status` (active/limited/self_excluded/banned), `language`
(am/en/om/ti), `min_kyc_level`, `registered_after`, `registered_before`,
`active_since`. An empty object (`{}`) matches every *eligible* player.

**`self_excluded` and `banned` users are never resolved into any
audience, unconditionally** — this is applied after every filter clause
in `_build_where()`, not exposed as something a filter combination could
turn off, and confirmed to have no caller anywhere in this codebase other
than this feature's own audience count/send path. A responsible-gambling
finding during a production-readiness pass: leaving every filter blank
(the UI's own documented way to "reach every player") previously resolved
to a bare `WHERE true`, which included self-excluded and banned users —
a real player commitment (self-exclusion) must hold regardless of what an
admin's filter happened to say, not depend on someone remembering to
exclude them every time.

## Campaign lifecycle

```
draft --(send now)--> queued ----------------------\
draft --(schedule)--> scheduled --(time arrives)---> sending --> completed
scheduled --(reschedule)--> scheduled                            |-> partially_failed
scheduled/queued --(cancel)--> cancelled                         '-> failed
```

Enforced by `services/admin/notification_queries.py::_ALLOWED_TRANSITIONS`
— any other transition raises `InvalidCampaignTransition` (HTTP 409).
Editing a campaign's own content (`update_campaign_admin`) is only
allowed while it's still `draft`; once queued/scheduled/sending, the
audience may have already started resolving against that exact
title/body, so it can't be silently rewritten underneath a send in
progress. Use **Duplicate** to start a fresh draft from an existing
campaign instead.

`completed` vs `partially_failed` vs `failed` is decided once every
delivery row for that campaign reaches a terminal state
(delivered/failed/cancelled): all delivered → `completed`, all failed →
`failed`, a mix → `partially_failed`.

## The worker (`services/bot/campaign_worker.py`)

Runs inside the bot process (`services/bot/app.py`'s `_on_startup`),
alongside `notification_relay.run_forever()` — the same process already
holding the one shared `Notifier`/`Bot` instance, so campaign messages
go through the exact same rate pace and 429 backoff as every other
outbound message. Polls every `POLL_INTERVAL_SECONDS` (10s), dispatches
up to `DISPATCH_BATCH_SIZE` (200) pending deliveries per sending campaign
per tick.

One tick (`process_once`) does three things in order:

1. **Claim due campaigns**: `UPDATE ... WHERE status IN ('queued',
   'scheduled') AND (scheduled_at IS NULL OR scheduled_at <= now()) FOR
   UPDATE SKIP LOCKED` → `sending`. `SKIP LOCKED` means a horizontally
   scaled second worker process would never double-claim the same
   campaign, without a separate distributed lock.
2. **Resolve + dispatch every currently-`sending` campaign** (not just
   ones this tick just claimed): resolve the audience once
   (`recipient_count IS NOT NULL` guards against re-resolving), then
   enqueue every delivery still `pending` onto the Redis stream, marking
   it `processing` first.
3. **Finalize**: any `sending` campaign whose deliveries are all terminal
   moves to `completed`/`partially_failed`/`failed`.

### Crash safety

Step 2 re-scans **every** `sending` campaign on **every** tick, not just
freshly-claimed ones. Verified directly in `tests/integration/
test_notification_center.py::test_worker_resumes_a_partially_dispatched_
sending_campaign` by forcing a campaign into `sending` with no delivery
rows yet (simulating a crash the instant after claim) and confirming the
very next `process_once()` tick seeds and dispatches it correctly. That
covers a crash anywhere before a delivery row is marked `processing`.

Within `_dispatch_pending_deliveries()`, marking a delivery `processing`
(a Postgres `UPDATE`) and actually enqueueing it (a Redis `XADD` via
`enqueue_campaign_message()`) are two separate operations against two
separate systems — not one atomic step. That ordering was chosen
deliberately: this query only ever selects `pending` rows, so a row
already marked `processing` is never re-enqueued by *this* loop, which
means a crash here can never send the same message twice **from this
path alone**.

**The narrower gap this used to leave** — a crash landing in the exact
gap between the `UPDATE` committing and the `XADD` completing left that
one delivery row stuck at `processing` forever, with no automated
reclaim — is now closed. `_reclaim_stuck_deliveries()` (run every tick,
before dispatch) resets any `processing` row idle past
`RECLAIM_STUCK_AFTER_SECONDS` (15 minutes — deliberately generous, not
tightly tuned) back to `pending`, so the same tick's own dispatch pass
picks it up again through the normal path. This can, in principle,
enqueue a delivery twice (once before the crash, if the original `XADD`
had actually landed; once from the reclaim) — what makes that still never
become a real duplicate Telegram message is `notification_relay.py::
process_one()` re-checking the delivery's own live status immediately
before ever calling `notifier.send()`, and skipping outright if it's no
longer `processing` (i.e. an earlier stream entry for the same
`delivery_id` already resolved it). This relies on the relay's own
existing guarantee that one user's own stream entries are always
processed in order, never concurrently with each other (see
`_drain_one_user()`) — so a duplicate entry is only ever dequeued *after*
the original one has already reached a terminal state. Verified directly:
`test_reclaim_resets_a_delivery_stuck_at_processing_past_the_threshold`
(a real crash simulation, carried through the real relay to an actual
`delivered` outcome) and `test_relay_skips_a_duplicate_stream_entry_for_
an_already_delivered_delivery` (the idempotency check itself, in
isolation).

### Delivery outcome tracking

`services/bot/notifier.py`'s `Notifier` already resolved a message's
`done` future to `None` on completion, telling a caller nothing about
*how* it finished. Extended (backward compatible — no existing test
asserted on the resolved value) to resolve to `"delivered"`,
`"blocked"` (`TelegramForbiddenError` — the user blocked the bot),
`"gave_up"` (retries exhausted after repeated `TelegramRetryAfter`), or
`"failed"` (anything else). `notification_relay.py::process_one()` awaits
this outcome and, when the message carries a `delivery_id` (a campaign
message, never a transactional one), calls
`packages/core/campaigns.py::mark_delivery_outcome()` to record it —
`"delivered"` stays `"delivered"`, every other outcome collapses to a
`"failed"` delivery row with the specific outcome string as
`failure_reason`.

## Security boundaries

- **RBAC** (`services/admin/rbac.py`): `notifications:view`/`create`/
  `templates_manage`/`view_analytics`/`view_delivery_details` →
  ops+superadmin. `notifications:send`/`schedule`/`cancel` →
  **superadmin only** — the same "highest-leverage lever" reasoning
  already applied to `payments:configure` (a real send reaches every
  targeted player at once; drafting one doesn't). Finance and support
  gain **nothing** here — deliberately, per this system's own design
  goal of not silently widening unrelated roles just because a new
  feature exists.
- **Audience resolution is server-side only.** The admin UI's "Check
  audience size" button calls `POST /notifications/audience/count`,
  which runs the real query — the client never receives a user list to
  filter locally, and the send path re-resolves the audience itself
  independent of whatever count the UI last displayed.
- **Stored content is never executed server-side.** Campaign title/body
  are stored and returned verbatim (verified:
  `test_script_content_in_campaign_title_is_stored_and_returned_
  verbatim_not_executed`) — the real place this matters is the admin
  console's own history/detail screens rendering another admin's
  campaign content, which is why `web/admin/js/screens/notifications.js`
  runs every admin-authored field through `escapeHtml()` before
  inserting it into the DOM, the same convention every other admin
  screen already uses.
