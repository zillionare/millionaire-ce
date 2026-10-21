"""Unit tests for the broker-level triple-barrier overlay."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from quantide.service.risk_overlay import (
    TripleBarrierConfig,
    TripleBarrierOverlay,
)


class _FakeBroker:
    def __init__(self, positions: dict[str, object], bars: dict[tuple[str, datetime.date], dict]) -> None:
        self.positions = positions
        self.bars = bars
        self.exit_calls: list[dict] = []
        self.fail_exit = False

    def get_bar_for_date(self, asset: str, date: datetime.date):
        return self.bars.get((asset, date))

    async def execute_exit(self, asset: str, shares: int, price: float, order_time, reason: str = ""):
        if self.fail_exit:
            raise RuntimeError("limit down")
        self.exit_calls.append(
            {"asset": asset, "shares": shares, "price": price, "tm": order_time, "reason": reason}
        )
        pos = self.positions.get(asset)
        if pos is not None:
            pos.shares = max(0.0, pos.shares - shares)
            pos.avail = max(0.0, pos.avail - shares)
        return SimpleNamespace(trades=[object()])

    def _log(self, level, message, dt=None):
        return None


def _position(price: float, shares: int = 100, avail: int | None = None):
    return SimpleNamespace(
        shares=float(shares),
        avail=float(shares if avail is None else avail),
        price=price,
    )


def _bar(*, open_: float, high: float, low: float, close: float) -> dict[str, float]:
    return {"open": open_, "high": high, "low": low, "close": close}


def _seed(overlay: TripleBarrierOverlay, broker: _FakeBroker, date: datetime.date):
    overlay.sync_positions(broker.positions, date)


@pytest.mark.asyncio
async def test_gap_below_stop_sells_at_open_with_slippage() -> None:
    broker = _FakeBroker(
        {"A": _position(10.0)},
        {("A", datetime.date(2024, 6, 4)): _bar(open_=9.3, high=9.6, low=9.2, close=9.5)},
    )
    overlay = TripleBarrierOverlay(
        TripleBarrierConfig(down_pct=0.06, slippage=0.01)
    )
    _seed(overlay, broker, datetime.date(2024, 6, 3))

    await overlay.check_open(broker, datetime.datetime(2024, 6, 4, 9, 30))

    assert len(broker.exit_calls) == 1
    assert broker.exit_calls[0]["price"] == pytest.approx(9.3 * 0.99)
    assert broker.exit_calls[0]["reason"].startswith("触发三重障碍下轨止损")


@pytest.mark.asyncio
async def test_intraday_stop_sells_same_day_at_stop_price() -> None:
    today = datetime.date(2024, 6, 4)
    broker = _FakeBroker(
        {"A": _position(10.0)},
        {( "A", today): _bar(open_=10.0, high=10.1, low=9.39, close=9.5)},
    )
    overlay = TripleBarrierOverlay(TripleBarrierConfig(down_pct=0.06, slippage=0.0))
    _seed(overlay, broker, datetime.date(2024, 6, 3))

    await overlay.check_open(broker, datetime.datetime(2024, 6, 4, 9, 30))
    assert broker.exit_calls == []

    await overlay.check_intraday(broker, datetime.datetime(2024, 6, 4, 14, 55))

    assert len(broker.exit_calls) == 1
    assert broker.exit_calls[0]["price"] == pytest.approx(9.4)
    assert broker.exit_calls[0]["tm"].time() == datetime.time(14, 55)


@pytest.mark.asyncio
async def test_t0_barrier_is_deferred_to_t1_open() -> None:
    t0 = datetime.date(2024, 6, 4)
    t1 = datetime.date(2024, 6, 5)
    broker = _FakeBroker(
        {"A": _position(10.0, avail=0)},
        {
            ("A", t0): _bar(open_=10.0, high=10.1, low=9.0, close=9.2),
            ("A", t1): _bar(open_=9.8, high=10.0, low=9.7, close=9.9),
        },
    )
    overlay = TripleBarrierOverlay(TripleBarrierConfig(down_pct=0.06, slippage=0.0))
    _seed(overlay, broker, t0)

    await overlay.check_intraday(broker, datetime.datetime(2024, 6, 4, 15, 0))
    assert broker.exit_calls == []
    assert "A" in overlay._pending

    broker.positions["A"].avail = 100
    await overlay.check_open(broker, datetime.datetime(2024, 6, 5, 9, 30))

    assert len(broker.exit_calls) == 1
    assert broker.exit_calls[0]["price"] == pytest.approx(9.8)
    assert "T+1" in broker.exit_calls[0]["reason"]


@pytest.mark.asyncio
async def test_gap_above_take_profit_sells_at_open() -> None:
    broker = _FakeBroker(
        {"A": _position(10.0)},
        {("A", datetime.date(2024, 6, 4)): _bar(open_=11.6, high=11.8, low=11.4, close=11.7)},
    )
    overlay = TripleBarrierOverlay(TripleBarrierConfig(up_pct=0.15, slippage=0.0))
    _seed(overlay, broker, datetime.date(2024, 6, 3))

    await overlay.check_open(broker, datetime.datetime(2024, 6, 4, 9, 30))

    assert len(broker.exit_calls) == 1
    assert broker.exit_calls[0]["price"] == pytest.approx(11.6)
    assert "止盈" in broker.exit_calls[0]["reason"]


@pytest.mark.asyncio
async def test_intraday_take_profit_sells_same_day() -> None:
    today = datetime.date(2024, 6, 4)
    broker = _FakeBroker(
        {"A": _position(10.0)},
        {("A", today): _bar(open_=10.0, high=11.51, low=9.9, close=11.4)},
    )
    overlay = TripleBarrierOverlay(TripleBarrierConfig(up_pct=0.15, slippage=0.0))
    _seed(overlay, broker, datetime.date(2024, 6, 3))

    await overlay.check_open(broker, datetime.datetime(2024, 6, 4, 9, 30))
    await overlay.check_intraday(broker, datetime.datetime(2024, 6, 4, 15, 0))

    assert len(broker.exit_calls) == 1
    assert broker.exit_calls[0]["price"] == pytest.approx(11.5)


@pytest.mark.asyncio
async def test_both_barriers_hit_uses_open_closer_rule() -> None:
    today = datetime.date(2024, 6, 4)
    # open 10.0 is much closer to the 9.4 stop than to the 11.5 take-profit.
    broker = _FakeBroker(
        {"A": _position(10.0)},
        {("A", today): _bar(open_=10.0, high=11.6, low=9.3, close=10.5)},
    )
    overlay = TripleBarrierOverlay(
        TripleBarrierConfig(up_pct=0.15, down_pct=0.06, slippage=0.0)
    )
    _seed(overlay, broker, datetime.date(2024, 6, 3))

    await overlay.check_open(broker, datetime.datetime(2024, 6, 4, 9, 30))
    await overlay.check_intraday(broker, datetime.datetime(2024, 6, 4, 15, 0))

    assert len(broker.exit_calls) == 1
    assert broker.exit_calls[0]["price"] == pytest.approx(9.4)


@pytest.mark.asyncio
async def test_time_exit_sells_at_open_after_horizon() -> None:
    broker = _FakeBroker(
        {"A": _position(10.0)},
        {("A", datetime.date(2024, 6, 6)): _bar(open_=10.1, high=10.2, low=10.0, close=10.1)},
    )
    overlay = TripleBarrierOverlay(TripleBarrierConfig(horizon=3, slippage=0.0))
    _seed(overlay, broker, datetime.date(2024, 6, 3))
    _seed(overlay, broker, datetime.date(2024, 6, 4))
    _seed(overlay, broker, datetime.date(2024, 6, 5))

    await overlay.check_open(broker, datetime.datetime(2024, 6, 6, 9, 30))

    assert len(broker.exit_calls) == 1
    assert "时间到期" in broker.exit_calls[0]["reason"]


@pytest.mark.asyncio
async def test_sell_rejection_keeps_position_and_pending() -> None:
    t0 = datetime.date(2024, 6, 4)
    t1 = datetime.date(2024, 6, 5)
    broker = _FakeBroker(
        {"A": _position(10.0, avail=0)},
        {
            ("A", t0): _bar(open_=10.0, high=10.1, low=9.0, close=9.2),
            ("A", t1): _bar(open_=9.8, high=10.0, low=9.7, close=9.9),
        },
    )
    overlay = TripleBarrierOverlay(TripleBarrierConfig(down_pct=0.06, slippage=0.0))
    _seed(overlay, broker, t0)
    await overlay.check_intraday(broker, datetime.datetime(2024, 6, 4, 15, 0))
    broker.positions["A"].avail = 100
    broker.fail_exit = True

    await overlay.check_open(broker, datetime.datetime(2024, 6, 5, 9, 30))

    assert broker.exit_calls == []
    assert "A" in overlay._pending
    assert broker.positions["A"].shares == 100
