"""Unit tests for the live/paper triple-barrier risk strategy."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from quantide.strategies.triple_barrier_risk import TripleBarrierRiskStrategy


class _FakeBroker:
    portfolio_id = "host"

    def __init__(self, positions: dict[str, object], price: float) -> None:
        self.positions = positions
        self.price = price
        self.sell_calls: list[tuple[str, int, dict]] = []

    def get_prices(self, assets: list[str]) -> dict[str, float]:
        return {asset: self.price for asset in assets if asset in self.positions}

    async def sell(self, asset: str, shares: int, **kwargs):
        self.sell_calls.append((asset, int(shares), kwargs))
        pos = self.positions.get(asset)
        if pos is not None:
            pos.shares = max(0.0, pos.shares - shares)
            pos.avail = max(0.0, pos.avail - shares)
        return SimpleNamespace(trades=[])


def _pos(price: float = 10.0, shares: int = 100, avail: int | None = None):
    return SimpleNamespace(
        asset="A",
        shares=float(shares),
        avail=float(shares if avail is None else avail),
        price=price,
        mv=price * shares,
    )


def _strategy(broker: _FakeBroker, **config):
    return TripleBarrierRiskStrategy(
        broker,
        {"take_profit_pct": 0.15, "stop_loss_pct": 0.06, "horizon": 3, **config},
    )


@pytest.mark.asyncio
async def test_t0_stop_is_deferred_to_next_day() -> None:
    broker = _FakeBroker({"A": _pos(avail=0)}, price=9.0)
    strategy = _strategy(broker)
    logs: list[str] = []
    strategy.log = lambda msg, *args, **kwargs: logs.append(str(msg))

    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 3, 10, 0))

    assert broker.sell_calls == []
    assert "A" in strategy._pending

    broker.positions["A"].avail = 100
    broker.price = 9.8
    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 4, 9, 31))

    assert len(broker.sell_calls) == 1
    assert any("T+1" in message for message in logs)


@pytest.mark.asyncio
async def test_t1_stop_sells_at_market() -> None:
    broker = _FakeBroker({"A": _pos()}, price=10.0)
    strategy = _strategy(broker)
    logs: list[str] = []
    strategy.log = lambda msg, *args, **kwargs: logs.append(str(msg))
    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 3, 10, 0))

    broker.price = 9.39
    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 4, 10, 0))

    assert len(broker.sell_calls) == 1
    assert any("止损" in message for message in logs)


@pytest.mark.asyncio
async def test_take_profit_and_time_barrier() -> None:
    broker = _FakeBroker({"A": _pos()}, price=10.0)
    strategy = _strategy(broker)
    logs: list[str] = []
    strategy.log = lambda msg, *args, **kwargs: logs.append(str(msg))
    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 3, 10, 0))

    broker.price = 11.5
    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 4, 10, 0))
    assert any("止盈" in message for message in logs)

    broker2 = _FakeBroker({"A": _pos()}, price=10.0)
    strategy2 = _strategy(broker2)
    logs2: list[str] = []
    strategy2.log = lambda msg, *args, **kwargs: logs2.append(str(msg))
    for day in (3, 4, 5, 6):
        await strategy2.on_check(
            broker2.positions, datetime.datetime(2024, 6, day, 10, 0)
        )
    assert len(broker2.sell_calls) == 1
    assert any("时间到期" in message for message in logs2)


@pytest.mark.asyncio
async def test_no_sell_outside_trading_session() -> None:
    broker = _FakeBroker({"A": _pos()}, price=10.0)
    strategy = _strategy(broker)
    strategy.log = lambda msg, *args, **kwargs: None
    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 3, 10, 0))

    broker.price = 9.0
    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 4, 20, 0))
    assert broker.sell_calls == []

    await strategy.on_check(broker.positions, datetime.datetime(2024, 6, 5, 10, 0))
    assert len(broker.sell_calls) == 1


@pytest.mark.asyncio
async def test_pending_is_logged_once_per_day() -> None:
    broker = _FakeBroker({"A": _pos(avail=0)}, price=9.0)
    strategy = _strategy(broker)
    logs: list[str] = []
    strategy.log = lambda msg, *args, **kwargs: logs.append(str(msg))

    for minute in range(0, 6):
        await strategy.on_check(
            broker.positions, datetime.datetime(2024, 6, 3, 10, minute)
        )

    assert sum("风控挂起" in message for message in logs) == 1


def test_horizon_counts_trading_days(monkeypatch) -> None:
    from quantide.data.models.calendar import calendar

    entry = datetime.date(2024, 6, 7)
    today = datetime.date(2024, 6, 11)
    monkeypatch.setattr(
        calendar, "get_frames", lambda start, end, frame_type: [start, end]
    )
    assert TripleBarrierRiskStrategy._held_trading_days(entry, today) == 1
