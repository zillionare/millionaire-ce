"""Security regressions for CE authentication defaults."""

from __future__ import annotations

import pytest

from quantide.service.init_wizard import init_wizard
from quantide.web.auth.manager import AuthManager


@pytest.fixture
def isolated_auth_manager(tmp_path):
    """Yield a fresh AuthManager bound to an isolated database."""
    previous = AuthManager._instance
    AuthManager._instance = None
    manager = AuthManager(db_path=tmp_path / "auth.db")
    manager.initialize()
    try:
        yield manager
    finally:
        AuthManager._instance = previous


def _assert_no_default_admin(repo) -> None:
    assert repo.get_by_username("admin") is None
    assert repo.list_all() == []
    assert repo.authenticate("admin", "admin123") is None


def test_initialize_does_not_create_default_admin(isolated_auth_manager):
    """Auth storage starts empty; admin/admin123 is not a shipped account."""
    _assert_no_default_admin(isolated_auth_manager.user_repo)


def test_rebind_database_does_not_create_default_admin(
    isolated_auth_manager, tmp_path
):
    """Rebinding auth storage must not silently restore default credentials."""
    isolated_auth_manager.rebind_database(str(tmp_path / "rebound.db"))

    _assert_no_default_admin(isolated_auth_manager.user_repo)


def test_init_wizard_creates_admin_only_from_explicit_password(isolated_auth_manager):
    """The wizard step creates admin with the user-supplied password."""
    repo = isolated_auth_manager.user_repo
    assert repo.authenticate("admin", "admin123") is None

    init_wizard.save_admin_password("StrongPass!123")

    assert repo.authenticate("admin", "StrongPass!123") is not None
    assert repo.authenticate("admin", "admin123") is None
