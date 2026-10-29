"""系统维护 fragment 导航不能把 header/sidebar 重绘进 main。"""

from __future__ import annotations

from tests.e2e.support.system_settings_session import system_settings_e2e_session


def test_system_accounts_htmx_returns_main_and_oob_sidebar_only():
    with system_settings_e2e_session() as session:
        response = session.client.get(
            "/system/accounts",
            headers={"HX-Request": "true"},
            follow_redirects=True,
        )
    assert response.status_code == 200
    text = response.text
    assert 'id="layout-main-content"' in text
    assert "hx-swap-oob" in text
    assert "<header" not in text.lower()


def test_data_calendar_htmx_returns_main_and_oob_sidebar_only():
    with system_settings_e2e_session() as session:
        response = session.client.get(
            "/data/calendar",
            headers={"HX-Request": "true"},
            follow_redirects=True,
        )
    assert response.status_code == 200
    text = response.text
    assert 'id="layout-main-content"' in text
    assert "<header" not in text.lower()
