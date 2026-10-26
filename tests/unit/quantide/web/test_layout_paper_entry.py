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


def test_system_maintenance_menu_has_accounts_entry():
    """系统维护侧边栏包含账号管理入口。"""
    from quantide.web.layouts.main import SYSTEM_MAINTENANCE_MENU

    urls = {
        child["url"]
        for group in SYSTEM_MAINTENANCE_MENU
        for child in group.get("children", [])
    }
    assert "/system/accounts" in urls


def test_header_menu_paper_entry_not_gateway_disabled():
    """仿真入口不依赖网关，不应被 requires_gateway 置灰。"""
    from quantide.web.layouts.main import build_header_menu

    registry = BrokerRegistry()
    portfolio_id = "sim_nav_gateway"
    registry.register(
        BrokerKind.SIMULATION,
        portfolio_id,
        SimpleNamespace(portfolio_name="Paper 100万", status=True),
    )
    try:
        menu = build_header_menu(trade_enabled=False, req=None)
        paper = next(item for item in menu if item.get("title") == "仿真")
        live = next(item for item in menu if item.get("title") == "实盘")
        assert paper.get("requires_gateway") is not True
        assert paper.get("disabled") is not True
        assert live.get("requires_gateway") is True
    finally:
        registry.unregister(BrokerKind.SIMULATION, portfolio_id)
