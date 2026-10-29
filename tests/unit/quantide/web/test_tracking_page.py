"""dry-run 对照页基础渲染测试。"""

from __future__ import annotations

from tests.e2e.support.system_settings_session import system_settings_e2e_session


def test_tracking_page_renders_empty_state():
    with system_settings_e2e_session() as session:
        response = session.client.get("/tracking/unknown-account")
    assert response.status_code == 200
    assert "dry-run 对照" in response.text
    assert "暂无跟踪数据" in response.text
