"""Audience resolution: a fixed, backend-validated JSON filter shape turned
into a real parameterized query -- a client (admin UI) never supplies raw
SQL or a filter string, the same discipline packages/core/campaigns.py's
own Notification Center audience filter already established for Telegram
campaigns.

v1 filter shape (all keys optional, an empty dict means "everyone"):
    {"attributes": {"<key>": "<value>", ...}}   -- exact-match containment
      against sms_contacts.attributes (a real, parameterized `@>` JSONB
      containment query).

Suppression and opt-out are never part of the filter itself -- they are
unconditional exclusions applied by every caller of resolve_recipients,
so a campaign can never accidentally target a suppressed contact by
omitting a filter clause for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from packages.core.ledger import AsyncpgConnection

MAX_ATTRIBUTE_FILTER_KEYS = 20


class InvalidAudienceFilter(Exception):
    """The filter isn't the fixed shape this module accepts -- never a
    SQL/query-building error, since no caller-supplied value ever reaches
    a query string directly.
    """


@dataclass(frozen=True)
class Recipient:
    contact_id: int
    phone_e164: str
    display_name: str | None


def validate_audience_filter(audience_filter: dict[str, Any]) -> dict[str, Any]:
    """Raises InvalidAudienceFilter for anything outside the fixed shape.
    Returns the filter unchanged (a validation gate, not a transform) so
    callers can store exactly what was validated.
    """
    if not isinstance(audience_filter, dict):
        raise InvalidAudienceFilter("audience_filter must be an object")
    unknown_keys = set(audience_filter) - {"attributes"}
    if unknown_keys:
        raise InvalidAudienceFilter(f"unknown audience_filter keys: {sorted(unknown_keys)}")
    attributes = audience_filter.get("attributes")
    if attributes is not None:
        if not isinstance(attributes, dict):
            raise InvalidAudienceFilter("attributes must be an object")
        if len(attributes) > MAX_ATTRIBUTE_FILTER_KEYS:
            raise InvalidAudienceFilter(
                f"attributes filter has more than {MAX_ATTRIBUTE_FILTER_KEYS} keys"
            )
        for key, value in attributes.items():
            if not isinstance(key, str) or not isinstance(value, str | int | float | bool):
                raise InvalidAudienceFilter("attributes values must be strings, numbers, or booleans")
    return audience_filter


async def resolve_recipients(
    conn: AsyncpgConnection, *, tenant_id: int, audience_filter: dict[str, Any]
) -> list[Recipient]:
    """Real query, not an estimate -- used both for the campaign
    validation step's recipient_count and for the actual enqueue step, so
    the two are always computed from the identical logic (never allowed to
    drift into "the preview said N but M were actually sent").

    Suppressed and opted-out contacts are excluded unconditionally here --
    every caller gets this exclusion for free rather than needing to
    remember to apply it themselves.
    """
    validate_audience_filter(audience_filter)
    attributes = audience_filter.get("attributes")
    if attributes:
        rows = await conn.fetch(
            """
            SELECT c.id AS contact_id, c.phone_e164, c.display_name
            FROM sms_contacts c
            WHERE c.tenant_id = $1
              AND c.opted_out = false
              AND c.attributes @> $2::jsonb
              AND NOT EXISTS (
                SELECT 1 FROM sms_suppressions s
                WHERE s.tenant_id = c.tenant_id AND s.phone_e164 = c.phone_e164
              )
            ORDER BY c.id
            """,
            tenant_id,
            json.dumps(attributes),
        )
    else:
        rows = await conn.fetch(
            """
            SELECT c.id AS contact_id, c.phone_e164, c.display_name
            FROM sms_contacts c
            WHERE c.tenant_id = $1
              AND c.opted_out = false
              AND NOT EXISTS (
                SELECT 1 FROM sms_suppressions s
                WHERE s.tenant_id = c.tenant_id AND s.phone_e164 = c.phone_e164
              )
            ORDER BY c.id
            """,
            tenant_id,
        )
    return [
        Recipient(contact_id=row["contact_id"], phone_e164=row["phone_e164"], display_name=row["display_name"])
        for row in rows
    ]


async def is_suppressed(conn: AsyncpgConnection | asyncpg.Pool, *, tenant_id: int, phone_e164: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM sms_suppressions WHERE tenant_id = $1 AND phone_e164 = $2",
        tenant_id,
        phone_e164,
    )
    return row is not None
