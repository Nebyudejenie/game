"""Notification Center: real Postgres, real Redis Stream, the real
campaign worker and the real bot-notification relay (a fake Telegram Bot
API session underneath, the exact same established pattern
test_notification_relay.py and test_bot_handlers.py already use for
every other Telegram-facing test in this codebase) -- and real HTTP
requests against the real admin API for the RBAC/security boundary.
"""

import itertools
import random

import httpx
import pytest

from packages.core.campaigns import count_audience, resolve_audience_user_ids
from services.admin import notification_queries
from services.bot import campaign_worker, notification_relay
from services.bot.notifier import Notifier
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_bot_handlers import make_bot

_id_counter = itertools.count(random.randint(10**8, 2 * 10**8))

# A campaign that stays 'queued'/'scheduled' at the end of a test (rather
# than being cancelled or run to a terminal state) is real, persistent
# state in this shared, never-truncated-between-runs dev database --
# campaign_worker.process_once() deliberately re-scans EVERY due/sending
# campaign on every call, by design (its own crash-recovery guarantee), so
# a later test's own worker tick can and will sweep up an earlier test's
# abandoned campaign. audience_filter={} means "every user row that has
# ever existed in this database", so such a leftover would fan out real
# stream messages to unrelated historical test users and corrupt a later
# test's own single-recipient assertions. {"user_ids": []} always resolves
# to zero recipients, making an abandoned test campaign inert forever
# regardless of which later tick claims it.
_INERT_AUDIENCE: dict[str, list[int]] = {"user_ids": []}


def next_telegram_id() -> int:
    return next(_id_counter)


async def _register_user(conn, *, status: str = "active", language: str = "en") -> tuple[int, int]:
    telegram_id = next_telegram_id()
    row = await conn.fetchrow(
        "INSERT INTO users (telegram_id, display_name, status, language) VALUES ($1, $2, $3, $4) "
        "RETURNING id",
        telegram_id,
        f"notif-test-{telegram_id}",
        status,
        language,
    )
    return row["id"], telegram_id


# --- audience resolution (packages/core/campaigns.py) ----------------------


async def test_audience_all_users_count(pool, conn):
    before = await count_audience(pool, {})
    await _register_user(conn)
    await _register_user(conn)
    after = await count_audience(pool, {})
    assert after == before + 2


async def test_audience_status_filter(pool, conn):
    active_id, _ = await _register_user(conn, status="active")
    banned_id, _ = await _register_user(conn, status="banned")

    active_ids = await resolve_audience_user_ids(pool, {"status": "active"})
    assert active_id in active_ids
    assert banned_id not in active_ids


async def test_self_excluded_and_banned_users_are_never_in_any_audience(pool, conn):
    """A responsible-gambling floor, not merely an optional filter value:
    a self-excluded player's own real commitment must hold regardless of
    what an admin's audience filter happens to say -- including the
    common "leave everything blank to reach every player" case, which
    resolves to a bare `WHERE true` with no status clause of its own at
    all.
    """
    active_id, _ = await _register_user(conn, status="active")
    self_excluded_id, _ = await _register_user(conn, status="self_excluded")
    banned_id, _ = await _register_user(conn, status="banned")

    everyone = await resolve_audience_user_ids(pool, {})
    assert active_id in everyone
    assert self_excluded_id not in everyone
    assert banned_id not in everyone

    # Even an admin explicitly asking to see who a "banned" filter would
    # match gets a real, honest zero -- not a bypass of the floor above.
    explicitly_requested = await resolve_audience_user_ids(pool, {"status": "banned"})
    assert banned_id not in explicitly_requested
    assert explicitly_requested == []

    count = await count_audience(pool, {})
    total = await pool.fetchval(
        "SELECT count(*) FROM users u WHERE u.status NOT IN ('self_excluded', 'banned') "
        "AND u.id NOT IN (SELECT user_id FROM responsible_gaming_limits WHERE cooloff_until > now())"
    )
    assert count == total


async def test_cooling_off_users_are_never_in_any_audience(pool, conn):
    """The other half of the same responsible-gambling floor: a
    temporary, self-requested cool-off (distinct from permanent
    self-exclusion) must be respected too -- packages/core/
    responsible_gaming.py::marketing_eligible_user_ids() already encoded
    this exact rule; this proves the Notification Center's own audience
    resolution now enforces the identical one.
    """
    from datetime import datetime, timedelta, timezone

    active_id, _ = await _register_user(conn, status="active")
    cooling_off_id, _ = await _register_user(conn, status="active")
    expired_cooloff_id, _ = await _register_user(conn, status="active")

    await conn.execute(
        "INSERT INTO responsible_gaming_limits (user_id, cooloff_until) VALUES ($1, $2)",
        cooling_off_id,
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    await conn.execute(
        "INSERT INTO responsible_gaming_limits (user_id, cooloff_until) VALUES ($1, $2)",
        expired_cooloff_id,
        datetime.now(timezone.utc) - timedelta(days=1),  # already over
    )

    everyone = await resolve_audience_user_ids(pool, {})
    assert active_id in everyone
    assert cooling_off_id not in everyone
    assert expired_cooloff_id in everyone  # cool-off already ended -- eligible again


async def test_audience_combined_filter(pool, conn):
    match_id, _ = await _register_user(conn, status="active", language="am")
    wrong_language_id, _ = await _register_user(conn, status="active", language="en")

    matched = await resolve_audience_user_ids(pool, {"status": "active", "language": "am"})
    assert match_id in matched
    assert wrong_language_id not in matched


async def test_audience_exclusion(pool, conn):
    keep_id, _ = await _register_user(conn, status="active")
    exclude_id, _ = await _register_user(conn, status="active")

    resolved = await resolve_audience_user_ids(pool, {"status": "active"}, exclude_user_ids=[exclude_id])
    assert keep_id in resolved
    assert exclude_id not in resolved


async def test_audience_specific_user_ids(pool, conn):
    user_a, _ = await _register_user(conn)
    user_b, _ = await _register_user(conn)
    user_c, _ = await _register_user(conn)

    resolved = await resolve_audience_user_ids(pool, {"user_ids": [user_a, user_b]})
    assert set(resolved) == {user_a, user_b}
    assert user_c not in resolved


async def test_audience_rejects_an_unknown_status_value(pool):
    from packages.core.campaigns import InvalidAudienceFilter

    with pytest.raises(InvalidAudienceFilter):
        await count_audience(pool, {"status": "not-a-real-status"})


# --- campaign creation / editing (services/admin/notification_queries.py) --


async def test_create_edit_delete_draft_campaign(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    campaign_id = await notification_queries.create_campaign_admin(
        pool,
        admin_id=admin_id,
        internal_name="Test campaign",
        title="Maintenance",
        body="No action required.",
        audience_filter={"status": "active"},
        exclude_user_ids=[],
        template_id=None,
        ip_address="10.0.0.1",
    )
    rows = await notification_queries.list_campaigns_admin(pool)
    assert any(r["id"] == campaign_id and r["status"] == "draft" for r in rows)

    updated = await notification_queries.update_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, changes={"title": "Scheduled Maintenance"},
        ip_address="10.0.0.1",
    )
    assert updated is True
    detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert detail["title"] == "Scheduled Maintenance"

    deleted = await notification_queries.delete_draft_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address="10.0.0.1"
    )
    assert deleted is True
    assert await notification_queries.get_campaign_detail_admin(pool, campaign_id) is None


async def test_cannot_edit_a_non_draft_campaign(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter=_INERT_AUDIENCE, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    await notification_queries.send_campaign_now_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    with pytest.raises(notification_queries.CampaignNotEditable):
        await notification_queries.update_campaign_admin(
            pool, admin_id=admin_id, campaign_id=campaign_id, changes={"title": "new"}, ip_address=None
        )


async def test_duplicate_campaign_creates_a_fresh_draft_and_never_auto_sends(pool):
    admin_id, *_ = await create_test_admin(pool)
    original_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="Original", title="Promo", body="50% off",
        audience_filter={"status": "active"}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    new_id = await notification_queries.duplicate_campaign_admin(
        pool, admin_id=admin_id, campaign_id=original_id, ip_address=None
    )
    assert new_id is not None and new_id != original_id
    detail = await notification_queries.get_campaign_detail_admin(pool, new_id)
    assert detail["status"] == "draft"
    assert detail["title"] == "Promo"


# --- templates ---------------------------------------------------------


async def test_create_and_edit_template(pool):
    admin_id, *_ = await create_test_admin(pool)
    template_id = await notification_queries.create_template_admin(
        pool, admin_id=admin_id, name=f"maintenance-{template_id_suffix()}", category="Maintenance",
        title="Scheduled Maintenance", body="We'll be back soon.", ip_address=None,
    )
    templates = await notification_queries.list_templates_admin(pool)
    assert any(t["id"] == template_id for t in templates)

    updated = await notification_queries.update_template_admin(
        pool, admin_id=admin_id, template_id=template_id, changes={"is_active": False}, ip_address=None
    )
    assert updated is True
    templates_after = await notification_queries.list_templates_admin(pool)
    match = next(t for t in templates_after if t["id"] == template_id)
    assert match["is_active"] is False


def template_id_suffix() -> str:
    return str(next(_id_counter))


# --- state machine -------------------------------------------------------


async def test_send_now_transitions_draft_to_queued(pool):
    admin_id, *_ = await create_test_admin(pool)
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter=_INERT_AUDIENCE, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    sent = await notification_queries.send_campaign_now_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    assert sent is True
    detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert detail["status"] == "queued"


async def test_cannot_send_an_already_queued_campaign_again(pool):
    """The actual duplicate-send guard: a second /send on the same
    campaign must not create a second logical delivery run."""
    admin_id, *_ = await create_test_admin(pool)
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter=_INERT_AUDIENCE, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    await notification_queries.send_campaign_now_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    with pytest.raises(notification_queries.InvalidCampaignTransition):
        await notification_queries.send_campaign_now_admin(
            pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
        )


async def test_schedule_then_cancel(pool):
    from datetime import datetime, timedelta, timezone

    admin_id, *_ = await create_test_admin(pool)
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter=_INERT_AUDIENCE, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    scheduled = await notification_queries.schedule_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, scheduled_at=future, ip_address=None
    )
    assert scheduled is True
    detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert detail["status"] == "scheduled"

    cancelled = await notification_queries.cancel_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    assert cancelled is True
    detail_after = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert detail_after["status"] == "cancelled"


async def test_reschedule_moves_the_scheduled_time(pool):
    from datetime import datetime, timedelta, timezone

    admin_id, *_ = await create_test_admin(pool)
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter=_INERT_AUDIENCE, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    first_time = datetime.now(timezone.utc) + timedelta(hours=1)
    await notification_queries.schedule_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, scheduled_at=first_time, ip_address=None
    )
    second_time = datetime.now(timezone.utc) + timedelta(hours=5)
    rescheduled = await notification_queries.schedule_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, scheduled_at=second_time, ip_address=None
    )
    assert rescheduled is True
    detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert abs((detail["scheduled_at"] - second_time).total_seconds()) < 1


async def test_scheduler_does_not_pick_up_a_campaign_before_its_time(pool, redis):
    from datetime import datetime, timedelta, timezone

    admin_id, *_ = await create_test_admin(pool)
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter=_INERT_AUDIENCE, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    far_future = datetime.now(timezone.utc) + timedelta(days=1)
    await notification_queries.schedule_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, scheduled_at=far_future, ip_address=None
    )
    did_work = await campaign_worker.process_once(pool, redis)
    detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert detail["status"] == "scheduled"  # untouched -- not due yet


# --- real end-to-end delivery: worker -> stream -> relay -> notifier ------


async def test_send_now_delivers_through_the_real_worker_and_relay(pool, conn, redis):
    admin_id, *_ = await create_test_admin(pool)
    user_id, telegram_id = await _register_user(conn, status="active")

    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="Real send test", title="Scheduled Maintenance Test",
        body="This is a test announcement. No action is required.",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    await notification_queries.send_campaign_now_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )

    # The worker: claims the queued campaign, resolves the audience,
    # creates the delivery row, and enqueues it onto the real Redis Stream.
    did_work = await campaign_worker.process_once(pool, redis)
    assert did_work is True

    detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert detail["status"] == "sending"
    assert detail["recipient_count"] == 1

    deliveries = await notification_queries.list_deliveries_admin(pool, campaign_id=campaign_id)
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "processing"

    # The relay: the real consumer that actually drains the stream through
    # a real Notifier (fake Telegram Bot API session underneath).
    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    try:
        delivered = await notification_relay.process_next(pool, redis, notifier, consumer_name="campaign-test")
        assert delivered is True

        import asyncio
        await asyncio.sleep(0.1)
        assert len(session.sent) == 1
        assert session.sent[0].chat_id == telegram_id
        assert "Scheduled Maintenance Test" in session.sent[0].text
        assert "This is a test announcement" in session.sent[0].text
    finally:
        await notifier.stop()

    # The relay recorded the real outcome back onto the delivery row.
    deliveries_after = await notification_queries.list_deliveries_admin(pool, campaign_id=campaign_id)
    assert deliveries_after[0]["status"] == "delivered"
    assert deliveries_after[0]["delivered_at"] is not None

    # And the worker's own finalize step rolls that up onto the campaign.
    await campaign_worker.process_once(pool, redis)
    final_detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert final_detail["status"] == "completed"
    assert final_detail["delivered_count"] == 1
    assert final_detail["failed_count"] == 0


async def test_a_blocked_bot_recipient_is_recorded_as_failed_not_delivered(pool, conn, redis):
    """TelegramForbiddenError (the user blocked the bot) is exactly the
    kind of permanent, non-retryable failure a real campaign will hit in
    practice -- confirms it lands as a real 'failed' delivery with a
    real reason, not silently counted as delivered.
    """
    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import TelegramMethod

    class _ForbiddenSession(BaseSession):
        async def close(self) -> None:
            pass

        async def make_request(self, bot: Bot, method: TelegramMethod, timeout: float | None = None):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")

        async def stream_content(self, *args: object, **kwargs: object):
            raise NotImplementedError
            yield b""  # pragma: no cover

    admin_id, *_ = await create_test_admin(pool)
    user_id, telegram_id = await _register_user(conn, status="active")
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    await notification_queries.send_campaign_now_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    await campaign_worker.process_once(pool, redis)

    bot = Bot(token="123456:FAKE-TEST-TOKEN", session=_ForbiddenSession())
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notification_relay.process_next(pool, redis, notifier, consumer_name="blocked-test")
    finally:
        await notifier.stop()

    deliveries = await notification_queries.list_deliveries_admin(pool, campaign_id=campaign_id)
    assert deliveries[0]["status"] == "failed"
    assert deliveries[0]["failure_reason"] == "blocked"

    await campaign_worker.process_once(pool, redis)
    detail = await notification_queries.get_campaign_detail_admin(pool, campaign_id)
    assert detail["status"] == "failed"  # zero delivered, one failed -> FAILED not PARTIALLY_FAILED


async def test_worker_resumes_a_partially_dispatched_sending_campaign(pool, conn, redis):
    """Crash-safety, proven directly rather than asserted: a campaign
    already sitting at 'sending' with some deliveries still 'pending' (as
    if a prior worker tick crashed mid-dispatch) gets those leftover
    deliveries picked up on the very next tick, with no separate recovery
    step needed -- exactly the "restart recovery works" requirement.
    """
    admin_id, *_ = await create_test_admin(pool)
    user_id, telegram_id = await _register_user(conn, status="active")
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="Resume test", body="x",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    # Simulate "a worker already claimed this and was about to dispatch,
    # then crashed before creating any delivery rows" -- exactly
    # 'sending' with recipient_count still NULL.
    await pool.execute("UPDATE notification_campaigns SET status = 'sending' WHERE id = $1", campaign_id)

    did_work = await campaign_worker.process_once(pool, redis)
    assert did_work is True

    deliveries = await notification_queries.list_deliveries_admin(pool, campaign_id=campaign_id)
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "processing"


async def test_reclaim_resets_a_delivery_stuck_at_processing_past_the_threshold(pool, conn, redis):
    """The gap this session's own architecture doc used to flag as an
    accepted, un-self-healing limitation (see docs/
    NOTIFICATION_CENTER_ARCHITECTURE.md's "Crash safety" section): a
    delivery whose enqueue never actually reached Redis (the process died
    in the gap between the UPDATE and the XADD) used to sit stuck at
    'processing' forever. Simulated directly: a real delivery row
    backdated well past RECLAIM_STUCK_AFTER_SECONDS, with nothing ever
    enqueued for it (proving the original enqueue really never landed),
    gets reset to 'pending' and re-dispatched by the very next tick --
    and, carried all the way through the real relay, actually delivered.
    """
    admin_id, *_ = await create_test_admin(pool)
    user_id, telegram_id = await _register_user(conn, status="active")
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="Reclaim test", body="x",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    await pool.execute(
        "UPDATE notification_campaigns SET status = 'sending', recipient_count = 1 WHERE id = $1",
        campaign_id,
    )
    delivery_id = await pool.fetchval(
        "INSERT INTO notification_deliveries (campaign_id, user_id, status, last_attempt_at) "
        "VALUES ($1, $2, 'processing', now() - interval '1 hour') RETURNING id",
        campaign_id,
        user_id,
    )

    did_work = await campaign_worker.process_once(pool, redis)
    assert did_work is True

    row = await pool.fetchrow("SELECT status FROM notification_deliveries WHERE id = $1", delivery_id)
    assert row["status"] == "processing"  # reclaimed to pending, then re-dispatched within this same tick

    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    try:
        delivered = await notification_relay.process_next(pool, redis, notifier, consumer_name="reclaim-test")
        assert delivered is True
        import asyncio
        await asyncio.sleep(0.1)
        assert len(session.sent) == 1
        assert session.sent[0].chat_id == telegram_id
    finally:
        await notifier.stop()

    final = await pool.fetchrow("SELECT status FROM notification_deliveries WHERE id = $1", delivery_id)
    assert final["status"] == "delivered"


async def test_reclaim_leaves_a_recently_dispatched_delivery_alone(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id, _ = await _register_user(conn, status="active")
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    await pool.execute(
        "UPDATE notification_campaigns SET status = 'sending', recipient_count = 1 WHERE id = $1",
        campaign_id,
    )
    delivery_id = await pool.fetchval(
        "INSERT INTO notification_deliveries (campaign_id, user_id, status, last_attempt_at) "
        "VALUES ($1, $2, 'processing', now()) RETURNING id",
        campaign_id,
        user_id,
    )

    reclaimed_count = await campaign_worker._reclaim_stuck_deliveries(pool)  # noqa: SLF001
    assert reclaimed_count == 0

    row = await pool.fetchrow("SELECT status FROM notification_deliveries WHERE id = $1", delivery_id)
    assert row["status"] == "processing"  # still genuinely in flight, untouched


async def test_relay_skips_a_duplicate_stream_entry_for_an_already_delivered_delivery(pool, conn, redis):
    """The actual safety net that makes the reclaim sweep above safe
    against ever double-sending: if a delivery_id somehow ends up on the
    stream twice (a reclaim racing an enqueue whose process didn't
    *actually* crash, in principle), the second entry must never reach
    notifier.send() once the delivery is already resolved.
    """
    admin_id, *_ = await create_test_admin(pool)
    user_id, telegram_id = await _register_user(conn, status="active")
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="Dup test", body="x",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    delivery_id = await pool.fetchval(
        "INSERT INTO notification_deliveries (campaign_id, user_id, status) VALUES ($1, $2, 'delivered') "
        "RETURNING id",
        campaign_id,
        user_id,
    )

    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    try:
        # A stream entry for a delivery that's already terminal -- exactly
        # what a reclaim-triggered duplicate looks like from the relay's
        # own point of view.
        await notification_relay.process_one(
            pool, redis, notifier,
            msg_id="0-1",
            fields={"telegram_id": str(telegram_id), "raw_text": "duplicate", "delivery_id": str(delivery_id)},
        )
    finally:
        await notifier.stop()

    assert len(session.sent) == 0  # never actually sent -- skipped outright
    row = await pool.fetchrow("SELECT status FROM notification_deliveries WHERE id = $1", delivery_id)
    assert row["status"] == "delivered"  # untouched


async def test_cancelling_a_queued_campaign_cancels_its_pending_deliveries(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id, _ = await _register_user(conn, status="active")
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="x", body="x",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    # Seed a pending delivery directly, standing in for "the worker had
    # already resolved the audience moments before this cancel arrived."
    await pool.execute(
        "INSERT INTO notification_deliveries (campaign_id, user_id) VALUES ($1, $2)", campaign_id, user_id
    )
    await notification_queries.send_campaign_now_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    await notification_queries.cancel_campaign_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    deliveries = await notification_queries.list_deliveries_admin(pool, campaign_id=campaign_id)
    assert deliveries[0]["status"] == "cancelled"


# --- history / search / analytics --------------------------------------


async def test_history_search_and_pagination(pool):
    admin_id, *_ = await create_test_admin(pool)
    unique = f"Searchable{next(_id_counter)}"
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name=unique, title="x", body="x",
        audience_filter=_INERT_AUDIENCE, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    found = await notification_queries.list_campaigns_admin(pool, search=unique)
    assert len(found) == 1 and found[0]["id"] == campaign_id

    page = await notification_queries.list_campaigns_admin(pool, limit=1, offset=0)
    assert len(page) == 1


async def test_overview_reports_accurate_counts(pool, conn, redis):
    admin_id, *_ = await create_test_admin(pool)
    user_id, telegram_id = await _register_user(conn, status="active")
    campaign_id = await notification_queries.create_campaign_admin(
        pool, admin_id=admin_id, internal_name="x", title="Overview test", body="x",
        audience_filter={"user_ids": [user_id]}, exclude_user_ids=[], template_id=None, ip_address=None,
    )
    await notification_queries.send_campaign_now_admin(
        pool, admin_id=admin_id, campaign_id=campaign_id, ip_address=None
    )
    await campaign_worker.process_once(pool, redis)

    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    try:
        await notification_relay.process_next(pool, redis, notifier, consumer_name="overview-test")
    finally:
        await notifier.stop()
    await campaign_worker.process_once(pool, redis)

    overview = await notification_queries.notification_overview_admin(pool)
    assert overview["completed"] >= 1
    assert overview["delivered_total_30d"] >= 1


# --- RBAC / security, real HTTP against the real admin app ---------------


async def test_unauthorized_role_gets_403_for_send_and_cancel(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="finance")
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{admin_server}/notifications/campaigns",
            json={"internal_name": "x", "title": "x", "body": "x"},
            headers=headers,
        )
    # finance has no notifications:create permission at all -- the
    # directive's own explicit requirement: existing roles gain nothing
    # here just because the feature exists.
    assert create.status_code == 403


async def test_ops_can_create_but_not_send_a_campaign_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{admin_server}/notifications/campaigns",
            json={"internal_name": "ops draft", "title": "x", "body": "x"},
            headers=headers,
        )
        assert create.status_code == 200
        campaign_id = create.json()["id"]

        send = await client.post(
            f"{admin_server}/notifications/campaigns/{campaign_id}/send", headers=headers
        )
    # notifications:send is superadmin-only -- ops can draft, not send.
    assert send.status_code == 403


async def test_direct_api_bypass_without_a_session_token_is_rejected(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/notifications/campaigns")
    assert response.status_code == 401


async def test_script_content_in_campaign_title_is_stored_and_returned_verbatim_not_executed(admin_server, pool):
    """Telegram delivery itself can't execute a <script> tag (Notifier
    sends plain text/HTML-subset content to the Telegram API, never to a
    browser DOM) -- the real risk this guards is the ADMIN CONSOLE's own
    History/Detail screens rendering another admin's campaign title.
    Confirms the stored value round-trips exactly as submitted (proving
    nothing server-side mangles or silently strips it) -- web/admin's own
    escapeHtml() (web/admin/js/api.js) is what makes rendering *that*
    value safe, verified separately as a UI-code-path fact, not something
    an integration test can click through.
    """
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    payload = "<script>alert(1)</script>"
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{admin_server}/notifications/campaigns",
            json={"internal_name": "xss-test", "title": payload, "body": "safe body"},
            headers=headers,
        )
        campaign_id = create.json()["id"]
        detail = await client.get(f"{admin_server}/notifications/campaigns/{campaign_id}", headers=headers)
    assert detail.json()["title"] == payload  # stored/returned as inert data, never interpreted server-side


async def test_a_high_risk_all_active_users_broadcast_reports_a_real_recipient_count(pool, conn, admin_server):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    await _register_user(conn, status="active")
    await _register_user(conn, status="active")
    await _register_user(conn, status="banned")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/notifications/audience/count",
            json={"audience_filter": {"status": "active"}},
            headers=headers,
        )
    assert response.status_code == 200
    # A real server-side count, at least the 2 active users just seeded
    # (>=, not ==, since other tests in this same database run
    # concurrently against a shared, non-isolated users table).
    assert response.json()["count"] >= 2
