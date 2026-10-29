"""Regression coverage for isolated app boot and threaded init-wizard access."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from quantide.app_factory import create_app
from quantide.web.auth.manager import AuthManager


def test_isolated_app_boot_rebinds_singletons_before_init_wizard_request(tmp_path):
    """Each app's worker-thread wizard request must use that app's database."""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first_app = create_app(app_config_dir=first_dir, enforce_single_instance=False)
    with TestClient(first_app) as first_client:
        first_response = first_client.get("/init-wizard/", follow_redirects=False)
    assert first_response.status_code == 200

    second_app = create_app(app_config_dir=second_dir, enforce_single_instance=False)
    with TestClient(second_app) as second_client:
        second_response = second_client.get("/init-wizard/", follow_redirects=False)
    assert second_response.status_code == 200

    auth = AuthManager.get_instance()
    assert auth is not None
    assert Path(auth.auth_db.db_path) == (second_dir / "quantide.db")
