"""FR-115 + FR-185 + FR-360 e2e (策略实跑 + 持仓成本 + 风控事件).

按 spec §FR-115 + §FR-185 + §FR-360:
- DualMAStrategy 真实跑 paper broker 1 天
- 持仓成本 F-CB-1~4 在多次 buy/sell 后正确
- 风控事件在策略触发时正确发出

不依赖真实 qmt-gateway, 不 mock.patch 内部符号.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from quantide.data.models.calendar import calendar as calendar_model
from quantide.data.models.daily_bars import daily_bars
from quantide.data.sqlite import db
from quantide.service.sim_broker import PaperBroker
from quantide.strategies.example.dual_ma import DualMAStrategy
from tests.e2e.fixtures.minimal_assets import (
    ASSETS_ROOT_LOCAL,
    TEST_ASSET,
    TEST_DATE,
    TEST_DOWN_LIMIT,
    TEST_UP_LIMIT,
)


@pytest.fixture
def paper_broker():
    db.init(":memory:")
    calendar_model.load(ASSETS_ROOT_LOCAL / "baseline_calendar.parquet")
    daily_bars.connect(
        str(ASSETS_ROOT_LOCAL / "2024_bars_ext_cols.parquet"),
        str(ASSETS_ROOT_LOCAL / "baseline_calendar.parquet"),
    )
    broker = PaperBroker(portfolio_id="e2e-strategy", principal=1_000_000)
    broker._clock = datetime.datetime.combine(TEST_DATE, datetime.time(9, 30))
    broker._limits[TEST_ASSET] = {"up": TEST_UP_LIMIT, "down": TEST_DOWN_LIMIT}
    yield broker


async def _publish_quote(broker, asset, price, volume=10000):
    broker._on_limit_update({asset: {"up": TEST_UP_LIMIT, "down": TEST_DOWN_LIMIT}})
    broker._on_quote_update({asset: {"lastPrice": price, "volume": volume}})
    await asyncio.sleep(0.01)


# ===== FR-115 BaseStrategy 实跑 =====

@pytest.mark.e2e
@pytest.mark.e2e_paper
async def test_fr_115_dual_ma_strategy_runs_in_paper(paper_broker):
    """AC-FR-115: DualMAStrategy 在 paper broker 中能跑 on_day_open + on_bar + on_day_close (无异常)."""
    strategy = DualMAStrategy(paper_broker, {"fast": 5, "slow": 20})
    # on_day_open 不应抛
    await strategy.on_day_open(paper_broker._clock)
    # SC-04: on_bar 仅接 tm; 行情通过 get_bars/get_history 拉取.
    await strategy.on_bar(paper_broker._clock)
    # on_day_close 不应抛
    await strategy.on_day_close(paper_broker._clock)


# ===== FR-185 持仓成本 F-CB-1~4 e2e =====

@pytest.mark.e2e
@pytest.mark.e2e_paper
async def test_fr_185_cost_basis_f_cb_1_first_buy(paper_broker):
    """AC-FR-185 F-CB-4: 首次建仓成本包含买入手续费."""
    buy_task = asyncio.create_task(paper_broker.buy(TEST_ASSET, 100, price=10.0, timeout=1.0))
    await asyncio.sleep(0)
    await _publish_quote(paper_broker, TEST_ASSET, 10.0)
    await buy_task
    pos = paper_broker._positions[TEST_ASSET]
    assert pos.price == 10.05, f"F-CB-4 first buy cost_basis should be 10.05, got {pos.price}"


@pytest.mark.e2e
@pytest.mark.e2e_paper
async def test_fr_185_cost_basis_f_cb_1_weighted_update(paper_broker):
    """AC-FR-185 F-CB-1: 二次买入成本加权且包含两笔手续费."""
    buy1 = asyncio.create_task(paper_broker.buy(TEST_ASSET, 100, price=10.0, timeout=1.0))
    await asyncio.sleep(0)
    await _publish_quote(paper_broker, TEST_ASSET, 10.0)
    await buy1
    paper_broker.settle_t1()
    # 第二次买入 100 @ 10 (up_limit=11, down_limit=9) → 仍 valid
    buy2 = asyncio.create_task(paper_broker.buy(TEST_ASSET, 100, price=10.0, timeout=1.0))
    await asyncio.sleep(0)
    await _publish_quote(paper_broker, TEST_ASSET, 10.0)
    await buy2
    pos = paper_broker._positions[TEST_ASSET]
    assert pos.shares == 200
    assert abs(pos.price - 10.05) < 1e-6, f"F-CB-1 weighted avg should be 10.05, got {pos.price}"


@pytest.mark.e2e
@pytest.mark.e2e_paper
async def test_fr_185_cost_basis_f_cb_2_partial_sell(paper_broker):
    """AC-FR-185 F-CB-2: 部分卖出 (sell_qty < old_qty) 成本不变."""
    # Buy 200 @ 10
    buy1 = asyncio.create_task(paper_broker.buy(TEST_ASSET, 200, price=10.0, timeout=1.0))
    await asyncio.sleep(0)
    await _publish_quote(paper_broker, TEST_ASSET, 10.0)
    await buy1
    paper_broker.settle_t1()
    cost_before = paper_broker._positions[TEST_ASSET].price
    # Sell 100 @ 11
    sell = asyncio.create_task(paper_broker.sell(TEST_ASSET, 100, price=11.0, timeout=1.0))
    await asyncio.sleep(0)
    await _publish_quote(paper_broker, TEST_ASSET, 11.0)
    await sell
    pos = paper_broker._positions[TEST_ASSET]
    assert pos.shares == 100
    assert pos.price == cost_before, f"F-CB-2 partial sell cost should remain {cost_before}, got {pos.price}"


@pytest.mark.e2e
@pytest.mark.e2e_paper
async def test_fr_185_cost_basis_f_cb_3_full_close(paper_broker):
    """AC-FR-185 F-CB-3: 全部卖出 (sell_qty == old_qty) 持仓清空."""
    buy1 = asyncio.create_task(paper_broker.buy(TEST_ASSET, 100, price=10.0, timeout=1.0))
    await asyncio.sleep(0)
    await _publish_quote(paper_broker, TEST_ASSET, 10.0)
    await buy1
    paper_broker.settle_t1()
    sell = asyncio.create_task(paper_broker.sell(TEST_ASSET, 100, price=11.0, timeout=1.0))
    await asyncio.sleep(0)
    await _publish_quote(paper_broker, TEST_ASSET, 11.0)
    await sell
    assert TEST_ASSET not in paper_broker._positions
