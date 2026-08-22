"""Direct tests for services/bot/dedup.py -- indirectly proven already by
test_bot_handlers.py's duplicate-update test, this pins down the primitive
itself.
"""

import random

from services.bot.dedup import claim_update


async def test_first_claim_succeeds(redis):
    update_id = random.randint(10**9, 2 * 10**9)
    assert await claim_update(redis, update_id) is True


async def test_second_claim_of_same_id_fails(redis):
    update_id = random.randint(10**9, 2 * 10**9)
    assert await claim_update(redis, update_id) is True
    assert await claim_update(redis, update_id) is False
    assert await claim_update(redis, update_id) is False


async def test_different_ids_are_independent(redis):
    a, b = random.randint(10**9, 2 * 10**9), random.randint(10**9, 2 * 10**9)
    assert await claim_update(redis, a) is True
    assert await claim_update(redis, b) is True
