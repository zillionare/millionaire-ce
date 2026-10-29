"""dry-run tracking store / shadow broker 基础测试。"""

from __future__ import annotations

import datetime

import polars as pl
import pytest

from quantide.service.shadow_broker import ShadowBroker
from quantide.service.strategy_tracking import TrackingStore


def test_tracking_store_roundtrip(tmp_path):
    store = TrackingStore(tmp_path / "track.db")
    store.ensure_run(
        "paper:p1",
        "p1",
        start_date=datetime.date(2026, 10, 27),
        fill_timing="auction_close",
    )
    store.save_shadow_state("paper:p1", "p1", cash=100000, positions=[])
    store.save_daily(
        runtime_id="paper:p1",
        portfolio_id="p1",
        day=datetime.date(2026, 10, 27),
        source="dry_run",
        nav=100100,
        cash=90000,
        market_value=10100,
        positions=[{"asset": "000001.SZ", "shares": 100}],
        trades=[{"asset": "000001.SZ", "side": 1}],
        model_hash="m1",
    )
    store.save_daily(
        runtime_id="paper:p1",
        portfolio_id="p1",
        day=datetime.date(2026, 10, 27),
        source="actual",
        nav=100050,
        cash=90000,
        market_value=10050,
        positions=[],
    )
    rows = store.list_daily(runtime_id="paper:p1")
    assert len(rows) == 2
    assert {row["source"] for row in rows} == {"dry_run", "actual"}
    state = store.get_shadow_state("paper:p1")
    assert state is not None and state["cash"] == 100000


def test_shadow_broker_buy_and_sell_uses_backtest_close(monkeypatch):
    class _Bars:
        @staticmethod
        def get_bars_in_range(start, end=None, assets=None, adjust=None, eager_mode=True):
            return pl.DataFrame(
                {
                    "date": [datetime.date(2026, 10, 27)],
                    "asset": ["000001.SZ"],
                    "open": [10.0],
                    "high": [10.5],
                    "low": [9.8],
                    "close": [10.0],
                    "volume": [1000000.0],
                    "amount": [10_000_000.0],
                    "adjust": [1.0],
                    "is_st": [False],
                    "up_limit": [11.0],
                    "down_limit": [9.0],
                }
            )

    import quantide.service.shadow_broker as shadow_module

    monkeypatch.setattr(shadow_module, "daily_bars", _Bars())
    broker = ShadowBroker(
        portfolio_id="shadow",
        principal=100000,
        config={"commission": 1e-4, "stamp_duty": 5e-4, "transfer_fee": 1e-5, "min_commission": 5.0, "slippage_pct": 0.0},
        trade_date=datetime.date(2026, 10, 27),
    )
    broker.settle_t1()

    import asyncio

    result = asyncio.run(broker.buy_amount("000001.SZ", 10000))
    assert result.trades
    assert "000001.SZ" in broker.positions
    assert broker.positions["000001.SZ"].shares == 1000
    assert broker.trades[0]["price"] == pytest.approx(10.0)

    position = broker.positions["000001.SZ"]
    position.avail = position.shares
    sold = asyncio.run(broker.sell("000001.SZ", 500))
    assert sold.trades
    assert broker.positions["000001.SZ"].shares == 500
