"""Shared theme assets must be emitted once per full page load."""

from __future__ import annotations

import pytest

from tests.e2e.support.system_settings_session import system_settings_e2e_session


@pytest.mark.parametrize("path", ["/factors/", "/strategy"])
def test_primary_pages_include_shared_assets_once(path: str) -> None:
    with system_settings_e2e_session() as session:
        response = session.client.get(path)

    assert response.status_code == 200
    html = response.text
    assert html.count("cdn.tailwindcss.com") == 1
    assert html.count("unpkg.com/htmx.org") == 1
    assert html.count("franken-ui@2.0.0/dist/css/core.min.css") == 1
    assert html.count("--brand: #e41815") == 1
