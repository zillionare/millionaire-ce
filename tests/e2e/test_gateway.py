"""L2 gateway and degradation UI journeys."""

from __future__ import annotations

import pytest

from quantide.core.enums import OrderSide
from quantide.core.ports import OrderRequest
from quantide.core.runtime.gateway_broker import GatewayBrokerAdapter
from quantide.core.runtime.gateway_client import GatewayClient
from tests.e2e.pages import SystemPage, TradePage
from tests.e2e.support.gateway_stub import GatewayScenario, running_gateway_stub


@pytest.mark.e2e
@pytest.mark.e2e_gateway
def test_gateway_settings_page_can_test_and_save_configuration(gateway_session):
    """CE keeps gateway settings unavailable even when a stub is running."""
    page = SystemPage(gateway_session.client).open_gateway()
    assert page.status_code == 503
    page.require("实盘交易暂不可用", "CE edition 暂不支持实盘交易")


@pytest.mark.e2e
@pytest.mark.e2e_gateway
def test_ce_disables_live_trade_and_gateway_settings(gateway_session):
    """CE blocks both live trading and gateway settings routes."""
    live_page = TradePage(gateway_session.client).open_live()
    assert live_page.status_code == 503
    live_page.require("实盘交易暂不可用", "CE edition 暂不支持实盘交易")

    gateway_page = SystemPage(gateway_session.client).open_gateway()
    assert gateway_page.status_code == 503
    gateway_page.require("实盘交易暂不可用", "CE edition 暂不支持实盘交易")


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
@pytest.mark.e2e
@pytest.mark.e2e_gateway
@pytest.mark.private_only
async def test_gateway_reject_response_does_not_create_local_trade_state():
    """AC-FR0180-10 AC-FR0420-09: 网关拒单时不产生本地委托/成交残留."""
    scenario = GatewayScenario(asset={"principal": 200000, "total": 200000, "cash": 200000, "market_value": 0, "frozen_cash": 0}, reject_next_orders=["price out of range"])
    with running_gateway_stub(prefix="/qmt", scenario=scenario) as stub:
        adapter = GatewayBrokerAdapter(
            GatewayClient(stub.base_url, username="u", password="p", timeout=2)
        )
        ack = await adapter.submit(
            OrderRequest(
                asset="000001.SZ",
                side=OrderSide.BUY,
                value=100,
                price=10.0,
                extra={"qtoid": "shield-gateway-reject"},
            )
        )

        assert ack.order_id is None
        assert ack.status == "rejected"
        assert "price out of range" in ack.message
        assert adapter.query_orders() == []
        assert adapter.query_trades() == []
