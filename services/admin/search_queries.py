"""Global admin search (Phase 4, Section 6): one query fanning out to
several already-indexed tables, never a second search engine/database.
Every category is included only if the requesting admin's role already
holds that category's own existing view permission (services/admin/
rbac.py) -- the exact same boundary every dedicated screen already
enforces, not a new, parallel authorization concept invented for search.

Deliberately narrow for a first real version: exact-id lookups (fast,
index-backed) plus ILIKE substring matches on the handful of text columns
an operator would actually type (username, display name, room code,
payment reference, command name, audit action). No new index, no full-
text search engine -- "start with indexed PostgreSQL queries" (Section
6's own instruction), and this system's real table sizes today don't
need more than that yet.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import asyncpg

from services.admin.rbac import has_permission
from services.bot.phone import normalize_ethiopian_phone

_RESULT_LIMIT = 10


def _numeric(query: str) -> int | None:
    stripped = query.strip()
    if stripped.isdigit():
        return int(stripped)
    return None


async def _search_users(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    from packages.core.phone_crypto import phone_lookup_hash

    conditions = ["username ILIKE '%' || $1 || '%'", "display_name ILIKE '%' || $1 || '%'"]
    params: list[Any] = [query]
    if numeric is not None:
        params.append(numeric)
        conditions.append(f"id = ${len(params)}")
        params.append(numeric)
        conditions.append(f"telegram_id = ${len(params)}")
    normalized_phone = normalize_ethiopian_phone(query)
    if normalized_phone is not None:
        params.append(phone_lookup_hash(normalized_phone))
        conditions.append(f"phone_lookup_hash = ${len(params)}")

    # An exact id/telegram_id match must never be crowded out of the
    # LIMIT window by newer fuzzy username/display_name matches -- ranked
    # first, ahead of recency, whenever one exists.
    exact_order = "(id = $2 OR telegram_id = $3) DESC, " if numeric is not None else ""
    rows = await pool.fetch(
        f"""
        SELECT id, telegram_id, username, display_name, status
        FROM users
        WHERE {' OR '.join(conditions)}
        ORDER BY {exact_order}id DESC
        LIMIT {_RESULT_LIMIT}
        """,
        *params,
    )
    return [
        {
            "type": "user",
            "id": r["id"],
            "label": r["display_name"] or (r["username"] and f"@{r['username']}") or f"user #{r['id']}",
            "subtitle": f"telegram_id={r['telegram_id']} · {r['status']}",
            "screen": "users",
        }
        for r in rows
    ]


async def _search_rooms(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    conditions = ["code ILIKE '%' || $1 || '%'"]
    params: list[Any] = [query]
    if numeric is not None:
        params.append(numeric)
        conditions.append(f"id = ${len(params)}")
    # Same reasoning as _search_users: an exact id match is a guaranteed-
    # relevant result and must outrank recency/active-status, not compete
    # with however many other rooms' codes happen to fuzzy-match too.
    exact_order = "(id = $2) DESC, " if numeric is not None else ""
    rows = await pool.fetch(
        f"""
        SELECT id, code, stake, is_active
        FROM rooms
        WHERE {' OR '.join(conditions)}
        ORDER BY {exact_order}is_active DESC, id DESC
        LIMIT {_RESULT_LIMIT}
        """,
        *params,
    )
    return [
        {
            "type": "room",
            "id": r["id"],
            "label": r["code"],
            "subtitle": f"stake {r['stake']} ETB · {'active' if r['is_active'] else 'inactive'}",
            "screen": "rooms",
        }
        for r in rows
    ]


async def _search_rounds(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    if numeric is None:
        return []
    rows = await pool.fetch(
        """
        SELECT r.id, r.seq, r.status, r.room_id, ro.code
        FROM rounds r JOIN rooms ro ON ro.id = r.room_id
        WHERE r.id = $1
        LIMIT $2
        """,
        numeric,
        _RESULT_LIMIT,
    )
    return [
        {
            "type": "round",
            "id": r["id"],
            "label": f"Round #{r['seq']} in {r['code']}",
            "subtitle": r["status"],
            "screen": "rounds",
        }
        for r in rows
    ]


async def _search_payments(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    conditions = ["p.our_ref ILIKE '%' || $1 || '%'", "p.provider_ref ILIKE '%' || $1 || '%'"]
    params: list[Any] = [query]
    if numeric is not None:
        params.append(numeric)
        conditions.append(f"p.id = ${len(params)}")
    exact_order = "(p.id = $2) DESC, " if numeric is not None else ""
    rows = await pool.fetch(
        f"""
        SELECT p.id, p.direction, p.provider, p.amount, p.status, p.our_ref, u.display_name
        FROM payments p JOIN users u ON u.id = p.user_id
        WHERE {' OR '.join(conditions)}
        ORDER BY {exact_order}p.id DESC
        LIMIT {_RESULT_LIMIT}
        """,
        *params,
    )
    return [
        {
            "type": "payment",
            "id": r["id"],
            "label": f"{r['direction']} {r['amount']} ETB via {r['provider']} — {r['display_name']}",
            "subtitle": f"{r['status']} · ref {r['our_ref']}",
            "screen": "payments",
        }
        for r in rows
    ]


async def _search_bonuses(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    if numeric is None:
        return []
    rows = await pool.fetch(
        """
        SELECT b.id, b.amount, b.status, u.display_name
        FROM bonuses b JOIN users u ON u.id = b.user_id
        WHERE b.id = $1
        LIMIT $2
        """,
        numeric,
        _RESULT_LIMIT,
    )
    return [
        {
            "type": "bonus",
            "id": r["id"],
            "label": f"Bonus #{r['id']} — {r['amount']} ETB for {r['display_name']}",
            "subtitle": r["status"],
            "screen": "bonuses",
        }
        for r in rows
    ]


async def _search_commands(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT handler_name, command, display_name, enabled
        FROM bot_commands
        WHERE handler_name ILIKE '%' || $1 || '%'
           OR command ILIKE '%' || $1 || '%'
           OR display_name ILIKE '%' || $1 || '%'
        ORDER BY sort_order
        LIMIT $2
        """,
        query,
        _RESULT_LIMIT,
    )
    return [
        {
            "type": "command",
            "id": r["handler_name"],
            "label": r["command"] and f"/{r['command']}" or r["display_name"],
            "subtitle": "enabled" if r["enabled"] else "disabled",
            "screen": "telegram_health",
        }
        for r in rows
    ]


async def _search_notifications(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    conditions = ["internal_name ILIKE '%' || $1 || '%'", "title ILIKE '%' || $1 || '%'"]
    params: list[Any] = [query]
    if numeric is not None:
        params.append(numeric)
        conditions.append(f"id = ${len(params)}")
    exact_order = "(id = $2) DESC, " if numeric is not None else ""
    rows = await pool.fetch(
        f"""
        SELECT id, internal_name, status
        FROM notification_campaigns
        WHERE {' OR '.join(conditions)}
        ORDER BY {exact_order}id DESC
        LIMIT {_RESULT_LIMIT}
        """,
        *params,
    )
    return [
        {
            "type": "notification",
            "id": r["id"],
            "label": r["internal_name"],
            "subtitle": r["status"],
            "screen": "notifications",
        }
        for r in rows
    ]


async def _search_audit(pool: asyncpg.Pool, query: str, numeric: int | None) -> list[dict[str, Any]]:
    conditions = [
        "action ILIKE '%' || $1 || '%'",
        "target_id ILIKE '%' || $1 || '%'",
        "reason ILIKE '%' || $1 || '%'",
    ]
    params: list[Any] = [query]
    if numeric is not None:
        params.append(numeric)
        conditions.append(f"id = ${len(params)}")
    exact_order = "(id = $2) DESC, " if numeric is not None else ""
    rows = await pool.fetch(
        f"""
        SELECT id, action, target_type, target_id, reason, created_at
        FROM admin_audit_log
        WHERE {' OR '.join(conditions)}
        ORDER BY {exact_order}id DESC
        LIMIT {_RESULT_LIMIT}
        """,
        *params,
    )
    return [
        {
            "type": "audit",
            "id": r["id"],
            "label": r["action"],
            "subtitle": (
                f"{r['target_type']}:{r['target_id']} · {r['created_at'].isoformat()}"
                + (f" · {r['reason']}" if r["reason"] else "")
            ),
            "screen": "audit",
        }
        for r in rows
    ]


_SearchFn = Callable[[asyncpg.Pool, str, "int | None"], Awaitable[list[dict[str, Any]]]]

# Category name -> (permission required, search function). A single,
# explicit table rather than scattering the RBAC check inline in each
# function -- adding a new searchable category later means one new line
# here, not a new place to remember the permission check. Every function
# above shares the exact same (pool, query, numeric) signature
# specifically so this dispatch loop can call all of them identically,
# even the ones (rounds/bonuses ignore query, commands ignores numeric)
# that only use part of it.
_CATEGORIES: dict[str, tuple[str, _SearchFn]] = {
    "users": ("users:view", _search_users),
    "rooms": ("rooms:view", _search_rooms),
    "rounds": ("rounds:view", _search_rounds),
    "payments": ("payments:view", _search_payments),
    "bonuses": ("bonuses:view", _search_bonuses),
    "commands": ("telegram:commands_view", _search_commands),
    "notifications": ("notifications:view", _search_notifications),
    "audit": ("audit:view", _search_audit),
}


async def global_search(pool: asyncpg.Pool, *, query: str, role: str) -> dict[str, list[dict[str, Any]]]:
    query = query.strip()
    if len(query) < 2:
        # Too short to search meaningfully (a 1-character ILIKE '%x%'
        # against every table would be slow and useless) -- an empty
        # result set, not an error, so the UI can just show "keep typing".
        return {}
    numeric = _numeric(query)

    results: dict[str, list[dict[str, Any]]] = {}
    for category, (permission, search_fn) in _CATEGORIES.items():
        if not has_permission(role, permission):
            continue
        matches = await search_fn(pool, query, numeric)
        if matches:
            results[category] = matches
    return results
