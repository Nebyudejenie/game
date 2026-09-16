"""One-shot CLI for provisioning the initial 10 named simulated-player
profiles -- same "genuinely missing piece, no way to create X without
out-of-band tooling" shape as services/admin/create_admin_cli.py.
Creating and funding named bot accounts is an operational action (an
admin choosing to stand up specific business records), not a schema
change, so this is a script, not a data migration -- there is no natural
admin to attribute the create/fund audit trail to inside a migration,
and services/admin/simulated_players_queries.py::create_simulated_player()
requires a real admin_id for exactly that reason.

Every created bot's status defaults to 'disabled', and
simulated_players_settings.enabled independently defaults to false (see
the migration) -- running this script alone can never make a bot join a
room. An admin still has to explicitly enable the system and start each
bot afterward, from the Simulated Players console screen or its API.

Run: `python -m services.admin.seed_simulated_players_cli --admin-id <id>`.
Idempotent: a bot whose display_name already exists among simulated
players is skipped rather than duplicated, so a partial or repeated run
is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from services.admin import simulated_players_queries as spq

# The 10 names from the original product spec -- real Ethiopian names,
# never reused for a genuine customer account.
SEED_NAMES: tuple[str, ...] = (
    "Hana Bekele",
    "Dawit Alemu",
    "Meron Tesfaye",
    "Abel Girma",
    "Selamawit Kebede",
    "Yonatan Haile",
    "Eden Worku",
    "Natnael Tadesse",
    "Rahel Getachew",
    "Samuel Mekonnen",
)


async def _seed(admin_id: int) -> list[tuple[str, int | None, str]]:
    settings = get_settings()
    pool = await create_pool(dsn=settings.database_url, min_size=1, max_size=1)
    results: list[tuple[str, int | None, str]] = []
    try:
        existing_names = {
            row["display_name"]
            for row in await pool.fetch(
                "SELECT u.display_name FROM simulated_players sp JOIN users u ON u.id = sp.user_id"
            )
        }
        for index, name in enumerate(SEED_NAMES):
            if name in existing_names:
                results.append((name, None, "skipped (already exists)"))
                continue
            # Spread across the 4 strategies rather than defaulting every
            # bot to the same one -- a fresh 10-bot roster with visibly
            # different pacing from the very first Start is a better
            # demonstration of the feature than 10 identical "normal" bots.
            strategy = spq.STRATEGIES[index % len(spq.STRATEGIES)]
            try:
                user_id = await spq.create_simulated_player(
                    pool, admin_id=admin_id, display_name=name, strategy=strategy, ip_address=None
                )
                results.append((name, user_id, f"created (strategy={strategy})"))
            except spq.SimulatedPlayerRosterFull:
                results.append((name, None, "skipped (roster already at cap)"))
    finally:
        await pool.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed the initial 10 named Simulated Players (each funded with a "
        f"{spq.INITIAL_SIMULATED_BALANCE} ETB house-backed balance, created disabled)."
    )
    parser.add_argument("--admin-id", required=True, type=int, help="Real admin_users.id to attribute this to.")
    args = parser.parse_args()

    try:
        results = asyncio.run(_seed(args.admin_id))
    except Exception as exc:
        print(f"Seeding failed: {exc}", file=sys.stderr)
        return 1

    for name, user_id, outcome in results:
        label = f"user_id={user_id}" if user_id is not None else "no account"
        print(f"{name}: {outcome} ({label})")

    created = sum(1 for _, user_id, _ in results if user_id is not None)
    print(f"\n{created} bot(s) created. Every bot starts disabled, and the global "
          "switch stays off until an admin enables it from the Simulated Players screen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
