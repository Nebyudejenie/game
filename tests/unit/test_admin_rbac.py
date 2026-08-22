import pytest

from services.admin.rbac import PERMISSIONS, has_permission


def test_superadmin_has_every_permission():
    for permission in PERMISSIONS:
        assert has_permission("superadmin", permission), permission


def test_support_cannot_adjust_balances():
    assert not has_permission("support", "users:adjust_balance")


def test_support_can_view_users():
    assert has_permission("support", "users:view")


def test_finance_can_adjust_balances_but_not_manage_rooms():
    assert has_permission("finance", "users:adjust_balance")
    assert not has_permission("finance", "rooms:manage")


def test_ops_can_manage_rooms_but_not_adjust_balances():
    assert has_permission("ops", "rooms:manage")
    assert not has_permission("ops", "users:adjust_balance")


def test_only_superadmin_can_view_audit_log():
    for role in ("support", "finance", "ops"):
        assert not has_permission(role, "audit:view")
    assert has_permission("superadmin", "audit:view")


def test_unknown_permission_raises():
    with pytest.raises(ValueError):
        has_permission("superadmin", "not_a_real_permission")


def test_no_role_is_granted_permissions_it_was_not_explicitly_given():
    # Regression guard against a future permission being added without
    # deliberately deciding which roles get it -- every permission's role
    # set must be an explicit, non-empty choice.
    for permission, roles in PERMISSIONS.items():
        assert roles, f"{permission} has no roles assigned"
        assert roles <= {"support", "finance", "ops", "superadmin"}
