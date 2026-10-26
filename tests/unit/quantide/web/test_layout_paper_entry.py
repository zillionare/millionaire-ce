"""导航「仿真」入口的账户读取测试。"""

from __future__ import annotations

from types import SimpleNamespace

from quantide.core.enums import BrokerKind
from quantide.service.registry import BrokerRegistry
from quantide.web.layouts.main import _build_paper_entry


def test_paper_entry_uses_singleton_when_request_missing():
    """页面 render() 不传 req 时，仍从单例注册表读取仿真账户。"""
    registry = BrokerRegistry()
    portfolio_id = "sim_nav_unit"
    registry.register(
        BrokerKind.SIMULATION,
        portfolio_id,
        SimpleNamespace(portfolio_name="Paper 100万", status=True),
    )
    try:
        entry = _build_paper_entry(None)
        assert entry.get("disabled") is not True
        if "children" in entry:
            urls = [child["url"] for child in entry["children"]]
            names = [child["title"] for child in entry["children"]]
            assert any(portfolio_id in url for url in urls)
            assert "Paper 100万" in names
        else:
            assert portfolio_id in str(entry["url"])
    finally:
        registry.unregister(BrokerKind.SIMULATION, portfolio_id)
