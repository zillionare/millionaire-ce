"""回测成交对（FIFO 配对）服务测试。"""

import datetime

import polars as pl
import pytest

from quantide.core.enums import OrderSide
from quantide.data.models.entities import Trade
from quantide.service import trade_pairs as trade_pairs_module
from quantide.service.trade_pairs import build_trade_pairs


def _trade(
    *,
    tid: str,
    asset: str = "000001.SZ",
    side: OrderSide = OrderSide.BUY,
    tm: datetime.datetime,
    shares: float = 100.0,
    price: float = 10.0,
    fee: float = 5.0,
) -> Trade:
    return Trade(
        portfolio_id="pf-1",
        tid=tid,
        qtoid=f"q-{tid}",
        foid=f"f-{tid}",
        asset=asset,
        shares=shares,
        price=price,
        amount=price * shares,
        tm=tm,
        side=side,
        cid=f"c-{tid}",
        fee=fee,
    )


def _frame(trades: list[Trade]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "tid": [t.tid for t in trades],
            "portfolio_id": [t.portfolio_id for t in trades],
            "asset": [t.asset for t in trades],
            "side": [int(t.side) for t in trades],
            "price": [t.price for t in trades],
            "shares": [t.shares for t in trades],
            "fee": [t.fee for t in trades],
            "tm": [t.tm for t in trades],
        }
    )


def _install(monkeypatch, trades: list[Trade], positions: pl.DataFrame | None = None) -> None:
    monkeypatch.setattr(
        trade_pairs_module.db, "trades_all", lambda portfolio_id: _frame(trades)
    )
    monkeypatch.setattr(
        trade_pairs_module.db,
        "query_positions",
        lambda **kwargs: positions if positions is not None else pl.DataFrame(),
    )


def test_build_trade_pairs_returns_empty_without_trades(monkeypatch) -> None:
    _install(monkeypatch, [])

    assert build_trade_pairs("pf-1") == []


def test_build_trade_pairs_matches_buy_and_sell(monkeypatch) -> None:
    buy = _trade(tid="b1", tm=datetime.datetime(2024, 1, 2, 9, 30))
    sell = _trade(
        tid="s1",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 1, 10, 14, 55),
        price=12.0,
    )
    _install(monkeypatch, [buy, sell])

    pairs = build_trade_pairs("pf-1")

    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.buy_tid == "b1"
    assert pair.sell_tid == "s1"
    assert pair.shares == pytest.approx(100.0)
    assert pair.buy_amount == pytest.approx(1000.0)
    assert pair.sell_amount == pytest.approx(1200.0)
    assert pair.fee == pytest.approx(10.0)
    assert pair.pnl == pytest.approx(1200.0 - 1000.0 - 10.0)
    assert pair.return_rate == pytest.approx(190.0 / 1005.0)
    assert pair.holding_days == 8
    assert pair.status == "closed"


def test_build_trade_pairs_keeps_unclosed_buy_as_open_pair(monkeypatch) -> None:
    buy = _trade(tid="b1", tm=datetime.datetime(2024, 1, 2, 9, 30))
    positions = pl.DataFrame(
        {
            "portfolio_id": ["pf-1"],
            "dt": [datetime.date(2024, 1, 10)],
            "asset": ["000001.SZ"],
            "shares": [100.0],
            "avail": [100.0],
            "price": [10.0],
            "profit": [0.0],
            "mv": [1100.0],
        }
    )
    _install(monkeypatch, [buy], positions)

    pairs = build_trade_pairs("pf-1")

    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.status == "open"
    assert pair.sell_tid is None
    assert pair.pnl is None
    assert pair.unrealized_pnl == pytest.approx(100.0)


def test_build_trade_pairs_matches_fifo_across_assets(monkeypatch) -> None:
    buy_a = _trade(tid="a1", asset="000001.SZ", tm=datetime.datetime(2024, 1, 2, 9, 30))
    buy_b = _trade(tid="b1", asset="000002.SZ", tm=datetime.datetime(2024, 1, 2, 9, 30))
    sell_a = _trade(
        tid="s1",
        asset="000001.SZ",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 1, 10, 9, 30),
        price=11.0,
    )
    _install(monkeypatch, [buy_a, buy_b, sell_a])

    pairs = build_trade_pairs("pf-1")

    assert len(pairs) == 2
    closed = [pair for pair in pairs if pair.status == "closed"]
    opened = [pair for pair in pairs if pair.status == "open"]
    assert len(closed) == 1
    assert closed[0].buy_tid == "a1"
    assert closed[0].sell_tid == "s1"
    assert len(opened) == 1
    assert opened[0].buy_tid == "b1"


def test_build_trade_pairs_splits_partial_sell(monkeypatch) -> None:
    buy = _trade(tid="b1", tm=datetime.datetime(2024, 1, 2, 9, 30), shares=200.0)
    sell = _trade(
        tid="s1",
        side=OrderSide.SELL,
        tm=datetime.datetime(2024, 1, 10, 9, 30),
        shares=100.0,
        price=11.0,
    )
    _install(monkeypatch, [buy, sell])

    pairs = build_trade_pairs("pf-1")

    closed = [pair for pair in pairs if pair.status == "closed"]
    opened = [pair for pair in pairs if pair.status == "open"]
    assert len(closed) == 1
    assert closed[0].shares == pytest.approx(100.0)
    assert closed[0].fee == pytest.approx(7.5)
    assert len(opened) == 1
    assert opened[0].shares == pytest.approx(100.0)


def test_build_trade_pairs_sorts_by_buy_time(monkeypatch) -> None:
    buy_late = _trade(tid="b2", tm=datetime.datetime(2024, 2, 2, 9, 30))
    buy_early = _trade(tid="b1", tm=datetime.datetime(2024, 1, 2, 9, 30))
    _install(monkeypatch, [buy_late, buy_early])

    pairs = build_trade_pairs("pf-1")

    assert [pair.buy_tid for pair in pairs] == ["b1", "b2"]
