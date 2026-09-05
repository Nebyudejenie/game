# Notification Center — Admin Guide

**Notifications** in the left nav (`https://admin.arada.fun/console/`,
visible to **ops and superadmin only** — support and finance don't see
this tab, and a direct API call from either role gets a real `403`).
Sends real Telegram messages to players through the bot. There is no
email, SMS, push, or separate in-app inbox — if you need one of those,
it doesn't exist yet (see `docs/NOTIFICATION_CENTER_ARCHITECTURE.md`).

## Overview

The top of the screen shows the last 30 days: how many messages went out
today, how many campaigns sit in each stage (draft/scheduled/queued/
sending/completed/partially failed/failed/cancelled), total delivered,
and an overall delivery rate. The rate shows "no data yet" instead of a
fake 0% when nothing has sent in the window — it isn't lying to you when
the number legitimately doesn't exist.

## Creating a campaign

1. **New campaign** — internal name (for your own reference, players
   never see it), an optional template, the title and body players will
   actually read.
2. **Audience** — leave every filter blank to reach every player, or
   narrow it: status, language, minimum KYC level, registration/activity
   date ranges, or a specific comma-separated list of user IDs. Filled-in
   filters all apply together (a recipient has to match every one you
   set) — you can also exclude specific user IDs regardless of what
   matches. Click **Check audience size** before saving to see a real
   count from the database, not an estimate.
3. **Save as draft.** Nothing is sent yet — a draft has no recipients
   resolved and no effect on any player.

A draft is the only stage you can still edit. Once it's sent, scheduled,
or queued, its content is locked (see "Why can't I edit this?" below) —
duplicate it into a fresh draft instead if you need to change the
wording.

## Sending

Open a draft from the **Campaigns** table to see its **Send now**,
**Schedule for**, and **Delete draft** actions.

- **Send now** asks you to confirm (showing the resolved recipient count
  if one exists yet) — Send is superadmin-only, so an ops account will
  see a real error here, not a silent no-op. Once confirmed, delivery
  happens in the background; refresh the campaign to watch its status
  and delivered/failed counts move.
- **Schedule for** picks a future date/time — the campaign moves to
  `scheduled` and the worker picks it up automatically once that time
  arrives (checked roughly every 10 seconds). Scheduling and cancelling
  are also superadmin-only.
- A **scheduled** campaign can be rescheduled (pick a new time and click
  again) or **cancelled** any time before it starts sending.
- A **queued** campaign (sent via "Send now" but not yet picked up by a
  worker tick) can still be **cancelled**.
- Once a campaign reaches `sending`, it can no longer be stopped —
  recipients may already have received it.

**Duplicate** works on a campaign in any status and always creates a
fresh, independent draft — duplicating a campaign that already sent
never re-sends it.

## Why can't I edit this?

Once a campaign leaves `draft`, its content is what audience resolution
and delivery have already started working from (or are about to). Live-
editing it would mean some recipients got the old wording and others got
the new one, with no way to say which is "the real" campaign. Cancel and
duplicate into a new draft instead.

## Reading a campaign's detail page

Click any row in **Campaigns** to see its full title/body, resolved
audience filter, delivered/failed counts, and a **Deliveries** table —
one row per recipient, with their current status (pending / processing /
delivered / failed / retrying / cancelled) and, for a failed delivery,
why: `blocked` means that player has blocked the bot, `gave_up` means
Telegram kept rate-limiting the send until retries ran out, `failed`
covers anything else.

## Templates

A template pre-fills a campaign's title/body when you pick it from the
**New campaign** form — it's a starting point, not a live link; editing
a campaign after picking a template doesn't change the template, and
editing a template later doesn't touch campaigns already created from
it. Deactivating a template just removes it from the picker for new
campaigns.

## What counts as a "high-risk" send

Any **Send now** targeting a broad or unfiltered audience reaches real
players immediately and can't be recalled once messages are in flight —
that's exactly why Send/Schedule/Cancel are superadmin-only and every
send asks for an explicit confirmation showing the real recipient count
first. When testing a new campaign, target it at your own user ID (or a
small handful of known test accounts) via the "Specific user IDs"
audience filter rather than sending to everyone.
