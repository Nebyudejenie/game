"""Role-based access control (spec section 33/35: least privilege).

Four roles, per the spec's own admin panel section: support, finance, ops,
superadmin. Permissions are additive per role -- there is no hidden "god
mode" bypass; superadmin is just the role every permission happens to be
assigned to, checked through the exact same function as everyone else.
"""

from __future__ import annotations

# Matches admin_users.role's own CHECK constraint exactly (migrations/
# versions/1c85c3d09653_admin_console.py's ADMIN_ROLES) -- kept here
# rather than imported from that migration (migrations are one-off,
# frozen-in-time scripts, never a runtime dependency for app code) so
# services/admin/queries.py's admin-account provisioning can validate a
# requested role before insert instead of surfacing a raw DB constraint
# violation as a 500.
KNOWN_ADMIN_ROLES = frozenset({"support", "finance", "ops", "superadmin"})

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
    # Narrower than payments:view on purpose (spec section 97: least-
    # privilege raw-SMS access) -- support/ops can see a Telebirr evidence
    # row's status and amount like any other payment, but the raw SMS text
    # itself (payer's phone number fragment, exact wording) is finance/
    # superadmin only.
    "payments:view_raw_evidence": frozenset({"finance", "superadmin"}),
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
    # Notification Center: deliberately does NOT include finance or
    # support -- messaging every player is an operational (ops) concern
    # (maintenance windows, game announcements), not a financial-review
    # or player-support one, and existing roles gain nothing here just
    # because the feature exists. Drafting/viewing is ops+superadmin;
    # actually causing a real send is superadmin-only, the same
    # "highest-leverage lever" reasoning payments:configure already uses
    # -- a real broadcast reaches every targeted player at once, the
    # same blast-radius shape as redirecting where deposits get paid.
    "notifications:view": frozenset({"ops", "superadmin"}),
    "notifications:create": frozenset({"ops", "superadmin"}),
    "notifications:send": frozenset({"superadmin"}),
    "notifications:schedule": frozenset({"superadmin"}),
    "notifications:cancel": frozenset({"superadmin"}),
    "notifications:templates_manage": frozenset({"ops", "superadmin"}),
    "notifications:view_analytics": frozenset({"ops", "superadmin"}),
    "notifications:view_delivery_details": frozenset({"ops", "superadmin"}),
    # The single highest-leverage lever in the whole system, higher even
    # than payments:configure: this is what decides who *holds* every
    # other permission in this table, including this one. superadmin-only
    # on purpose -- finance/ops/support managing their own or each
    # other's accounts would mean a compromised lower-privilege account
    # could mint itself a fresh, unaudited-by-anyone-above-it identity.
    "admin_users:manage": frozenset({"superadmin"}),
    # Editing player-facing bot text (menu button labels, message
    # templates) is an operational/UX concern, not a financial one -- same
    # roles as notifications:templates_manage, the closest precedent
    # (both edit text real players see, neither moves money or grants
    # access to anyone else's account).
    "bot_content:manage": frozenset({"ops", "superadmin"}),
    # A bonus/referral row's existence and status is low-sensitivity --
    # same breadth as payments:view.
    "bonuses:view": frozenset({"support", "finance", "ops", "superadmin"}),
    # Configuring reward amounts/wagering/eligibility is operational
    # rule-authoring, not itself a money-movement action -- same roles as
    # notifications:templates_manage/bot_content:manage.
    "bonuses:manage_rules": frozenset({"ops", "superadmin"}),
    # A manual, ad-hoc grant directly credits a specific player's wallet
    # -- exactly users:adjust_balance's own shape and roles.
    "bonuses:grant": frozenset({"finance", "superadmin"}),
    # Same investigative audience as risk:view -- this is the referral-
    # specific extension of that same screen's fraud-signal philosophy.
    "bonuses:view_fraud_signals": frozenset({"ops", "finance", "superadmin"}),
}


def has_permission(role: str, permission: str) -> bool:
    allowed_roles = PERMISSIONS.get(permission)
    if allowed_roles is None:
        raise ValueError(f"unknown permission: {permission!r}")
    return role in allowed_roles
