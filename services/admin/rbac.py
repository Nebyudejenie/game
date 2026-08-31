"""Role-based access control (spec section 33/35: least privilege).

Four roles, per the spec's own admin panel section: support, finance, ops,
superadmin. Permissions are additive per role -- there is no hidden "god
mode" bypass; superadmin is just the role every permission happens to be
assigned to, checked through the exact same function as everyone else.
"""

from __future__ import annotations

PERMISSIONS: dict[str, frozenset[str]] = {
    "dashboard:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "users:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "users:adjust_balance": frozenset({"finance", "superadmin"}),
    "users:suspend": frozenset({"ops", "finance", "superadmin"}),
    # Same roles as payments:approve, not users:suspend -- KYC level is a
    # financial-compliance control (it gates withdrawal size), not a
    # user-standing one, even though both end up as a field on the same
    # users row.
    "users:verify_kyc": frozenset({"finance", "superadmin"}),
    "rounds:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "rounds:void": frozenset({"ops", "superadmin"}),
    "rooms:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "rooms:manage": frozenset({"ops", "superadmin"}),
    "reports:view": frozenset({"finance", "superadmin"}),
    "audit:view": frozenset({"superadmin"}),
    "payments:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "payments:approve": frozenset({"finance", "superadmin"}),
    # Narrower than payments:approve on purpose: approving one payment
    # bounds the blast radius of a bad call to that one request, but
    # toggling which rail is live or editing where manual deposits get
    # paid into changes behavior for every player at once -- the single
    # highest-leverage lever a compromised/rogue admin account could
    # pull (e.g. quietly redirecting the manual-deposit destination to a
    # personal account).
    "payments:configure": frozenset({"superadmin"}),
    # Same roles as rounds:void, not reports:view -- reading the risk
    # screen is an investigation tool for the roles who'd act on what it
    # shows (ops handles collusion/room abuse, finance handles payout
    # fraud), not a general reporting/analytics permission.
    "risk:view": frozenset({"ops", "finance", "superadmin"}),
}


def has_permission(role: str, permission: str) -> bool:
    allowed_roles = PERMISSIONS.get(permission)
    if allowed_roles is None:
        raise ValueError(f"unknown permission: {permission!r}")
    return role in allowed_roles
