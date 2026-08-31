"""Which payment rail(s) a player actually sees, computed once per call
from two independent facts that both have to be true, not just the
admin's own toggle: (1) an admin has switched the provider/direction on
in payment_provider_availability, and (2) the provider is actually
wired up with real, working code -- an enabled toggle alone can't make
a nonexistent adapter callable.

"chapa" and "manual" are the only two rails with a real implementation
in this codebase today (services/payments/chapa.py,
services/payments/manual.py/manual_provider.py) -- santimpay/arifpay
have a legal, migrated provider value and an admin-facing toggle
(payment_provider_availability seeds both as disabled) but no adapter
class exists for either, so they're hardcoded unavailable here
regardless of what the toggle says, until a real adapter is built. This
is the P1 directive's own launch principle in code: "the product must
be able to launch with ONE AUTOMATIC PROVIDER + MANUAL FALLBACK... do
not block on SantimPay and ArifPay."
"""

from __future__ import annotations

import asyncpg

from packages.core.config import Settings

_IMPLEMENTED_PROVIDERS = frozenset({"chapa", "manual"})


async def get_payment_availability(pool: asyncpg.Pool, settings: Settings) -> dict[str, list[str]]:
    """Returns {"deposit": [...provider names...], "withdraw": [...]},
    each entry a provider genuinely reachable for that direction right
    now. "manual" is unconditional once its own toggle is on -- it needs
    no external credentials, unlike chapa.
    """
    rows = await pool.fetch("SELECT provider, direction, enabled FROM payment_provider_availability")

    chapa_configured = bool(settings.chapa_api_key)
    chapa_deposit_configured = chapa_configured and bool(settings.public_base_url)

    availability: dict[str, list[str]] = {"deposit": [], "withdraw": []}
    for row in rows:
        provider: str = row["provider"]
        direction: str = row["direction"]
        enabled: bool = row["enabled"]
        if not enabled or provider not in _IMPLEMENTED_PROVIDERS:
            continue

        if provider == "chapa":
            reachable = chapa_deposit_configured if direction == "in" else chapa_configured
        else:
            reachable = True  # manual

        if reachable:
            key = "deposit" if direction == "in" else "withdraw"
            availability[key].append(provider)

    return availability
