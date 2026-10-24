"""FR-140 交易规则 — 价格（涨跌停）单元测试.

按 spec-trading.md §FR-140（2026-10 修订）:
- Paper/实盘不对涨跌停做交易限制：委托价超出 [down_limit, up_limit] 时**钳制**到边界，
  不拒单；撮合层也不再因涨停/跌停拒绝成交
- 市价单 (price == 0) 不受此校验
- 回测侧仍严格：缺少涨跌停数据时拒绝成交并告警

测试策略:
- paper_broker fixture + 通过 _on_limit_update 设置 _limits 字典 (绕过行情源)
- 调 buy()/sell() (async) 验证 price 校验
"""

from __future__ import annotations

import datetime

import pytest

from quantide.core.domain import QuoteSnapshot
from quantide.core.enums import OrderSide
from quantide.core.errors import (
    InsufficientPosition,
    NonMultipleOfLotSize,
    PriceOutOfLimit,
)
from quantide.data.sqlite import db
from quantide.service.sim_broker import PaperBroker

AC_TEST_DATE = datetime.datetime(2024, 6, 3, 9, 30, 0)
AC_ASSET = "000001.SZ"
AC_DOWN_LIMIT = 9.0
AC_UP_LIMIT = 11.0


class _DummyMarketData:
    def __init__(self):
        self._quotes: dict[str, QuoteSnapshot] = {}

    def set_quote(self, symbol: str, price: float, volume: float | None = None):
        self._quotes[symbol] = QuoteSnapshot(
            symbol=symbol,
            price=price,
            open=price,
            high=price,
            low=price,
            volume=volume,
            amount=(price * volume) if volume is not None else None,
            ts=datetime.datetime.now(),
        )

    def snapshot(self, symbols):
        return {s: self._quotes[s] for s in symbols if s in self._quotes}


@pytest.fixture
def paper_broker() -> PaperBroker:
    db.init(":memory:")
    return PaperBroker(
        portfolio_id="paper-unit",
        principal=100000,
        market_data=_DummyMarketData(),
        market_value_update_interval=0.0,
    )


def _set_limits(broker, asset: str = AC_ASSET, down: float = AC_DOWN_LIMIT, up: float = AC_UP_LIMIT) -> None:
    """设置 broker 的 _limits 字典, 模拟数据源推送涨跌停."""
    broker._on_limit_update({asset: {"up": up, "down": down}})


@pytest.fixture
def paper_broker_with_limits(paper_broker):
    """paper_broker + 设置 AC_ASSET 涨跌停 [9.0, 11.0]."""
    _set_limits(paper_broker)
    return paper_broker


@pytest.mark.asyncio
async def test_fr_140_limit_buy_within_range_accepted(paper_broker_with_limits):
    """AC-FR-140: 限价买价 10.0 (在 [9.0, 11.0] 内) 应被接受 (下单不抛 PriceOutOfLimit)."""
    paper_broker_with_limits.set_clock(AC_TEST_DATE)
    try:
        await paper_broker_with_limits.buy(asset=AC_ASSET, shares=100, price=10.0)
    except (NonMultipleOfLotSize, PriceOutOfLimit, InsufficientPosition) as e:
        pytest.fail(f"limit buy within range rejected: {e!r}")


@pytest.mark.asyncio
async def test_fr_140_limit_buy_above_up_clamped(paper_broker_with_limits):
    """AC-FR-140: 限价买价 11.5 (> up_limit 11.0) 钳制到 11.0，不拒单."""
    paper_broker_with_limits.set_clock(AC_TEST_DATE)
    await paper_broker_with_limits.buy(asset=AC_ASSET, shares=100, price=11.5)
    assert paper_broker_with_limits._active_orders[AC_ASSET][-1].price == 11.0


@pytest.mark.asyncio
async def test_fr_140_limit_buy_below_down_clamped(paper_broker_with_limits):
    """AC-FR-140: 限价买价 8.0 (< down_limit 9.0) 钳制到 9.0，不拒单."""
    paper_broker_with_limits.set_clock(AC_TEST_DATE)
    await paper_broker_with_limits.buy(asset=AC_ASSET, shares=100, price=8.0)
    assert paper_broker_with_limits._active_orders[AC_ASSET][-1].price == 9.0


@pytest.mark.asyncio
async def test_fr_140_limit_buy_at_boundary_accepted(paper_broker_with_limits):
    """AC-FR-140: 限价买价 == up_limit / down_limit (边界值) 应被接受 (闭区间)."""
    paper_broker_with_limits.set_clock(AC_TEST_DATE)
    try:
        await paper_broker_with_limits.buy(asset=AC_ASSET, shares=100, price=AC_UP_LIMIT)
    except PriceOutOfLimit as e:
        pytest.fail(f"buy at up_limit boundary rejected: {e!r}")
    paper_broker_with_limits._positions.pop(AC_ASSET, None)

    try:
        await paper_broker_with_limits.buy(asset=AC_ASSET, shares=100, price=AC_DOWN_LIMIT)
    except PriceOutOfLimit as e:
        pytest.fail(f"buy at down_limit boundary rejected: {e!r}")


@pytest.mark.asyncio
async def test_fr_140_market_order_skips_limit_check(paper_broker_with_limits):
    """AC-FR-140: 市价单 (price == 0) 不受涨跌停校验.

    市价单走撮合层 (lastPrice 撮合), 下单入口应不抛 PriceOutOfLimit.
    (后续撮合可能因开盘涨跌停不撮合, 但下单成功.)
    """
    paper_broker_with_limits.set_clock(AC_TEST_DATE)
    try:
        await paper_broker_with_limits.buy(asset=AC_ASSET, shares=100, price=0)
    except PriceOutOfLimit as e:
        pytest.fail(f"market order (price=0) raised PriceOutOfLimit: {e!r}")


def _seed_position(paper_broker_with_limits, asset: str = AC_ASSET, shares: int = 100, price: float = 10.0):
    paper_broker_with_limits.set_clock(AC_TEST_DATE)
    paper_broker_with_limits._apply_trade_to_portfolio(
        type("T", (), {
            "portfolio_id": "paper-unit", "tid": "t1", "qtoid": "o1", "foid": "f1",
            "asset": asset, "shares": shares, "price": price, "amount": shares * price,
            "tm": AC_TEST_DATE, "side": OrderSide.BUY, "cid": "c1", "fee": 0.0,
        })()
    )
    # T+1 未实施 (FR-160 待做), 这里手动 set avail 模拟 T+1 已过, 允许 sell
    paper_broker_with_limits._positions[asset].avail = shares


@pytest.mark.asyncio
async def test_fr_140_limit_sell_above_up_clamped(paper_broker_with_limits):
    """AC-FR-140: 限价卖价 12.0 (> up_limit) 钳制到 11.0，不拒单."""
    _seed_position(paper_broker_with_limits)
    await paper_broker_with_limits.sell(asset=AC_ASSET, shares=100, price=12.0)
    assert paper_broker_with_limits._active_orders[AC_ASSET][-1].price == 11.0


@pytest.mark.asyncio
async def test_fr_140_limit_sell_below_down_clamped(paper_broker_with_limits):
    """AC-FR-140: 限价卖价 7.0 (< down_limit) 钳制到 9.0，不拒单."""
    _seed_position(paper_broker_with_limits)
    await paper_broker_with_limits.sell(asset=AC_ASSET, shares=100, price=7.0)
    assert paper_broker_with_limits._active_orders[AC_ASSET][-1].price == 9.0


@pytest.mark.asyncio
async def test_fr_140_no_limit_data_no_check(paper_broker):
    """AC-140-01: 边界 - 没有涨跌停数据 (_limits 空, _get_price_limits 返回 (0, 0)), price 校验跳过."""
    paper_broker.set_clock(AC_TEST_DATE)
    try:
        await paper_broker.buy(asset="NO_LIMIT_ASSET", shares=100, price=999.0)
    except PriceOutOfLimit as e:
        pytest.fail(f"buy without limit data raised PriceOutOfLimit: {e!r}")


def test_get_price_limits_falls_back_to_limit_store(paper_broker, monkeypatch):
    """当日限价：hub 无数据时回落 Tushare 早晨落盘的 LimitPriceStore。"""

    class _Store:
        def get_limits(self, asset, dt):
            return 9.0, 11.0

    monkeypatch.setattr("quantide.data.get_limit_price_store", lambda: _Store())
    assert paper_broker._get_price_limits("000001.SZ") == (9.0, 11.0)
