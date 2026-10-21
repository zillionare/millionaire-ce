"""Wiring tests for the live counterpart of the triple-barrier overlay."""

from __future__ import annotations

import pytest

from quantide.service.strategy_runtime import StrategyRuntimeManager
from quantide.strategies.triple_barrier_risk import TripleBarrierRiskStrategy


class _Host:
    RISK_OVERLAY = "triple_barrier"

    @staticmethod
    def default_config() -> dict:
        return {"use_broker_barriers": True}


class _Broker:
    portfolio_id = "host"


def test_live_risk_strategy_is_attached_for_declared_overlay() -> None:
    manager = StrategyRuntimeManager()
    risk = manager._build_live_risk_strategy(_Host, _Broker(), {})

    assert isinstance(risk, TripleBarrierRiskStrategy)


def test_live_risk_strategy_can_be_disabled() -> None:
    manager = StrategyRuntimeManager()
    risk = manager._build_live_risk_strategy(
        _Host, _Broker(), {"use_broker_barriers": False}
    )

    assert risk is None


@pytest.mark.asyncio
async def test_strategy_loop_starts_and_stops_without_lifecycle_type_error(
    monkeypatch,
):
    """on_start/on_stop 只接受 self；runtime 不能传 timestamp (B2)."""
    import threading

    from quantide.service import strategy_runtime as runtime_module
    from quantide.service.strategy_runtime import StrategyRuntime

    class _FakeBroker:
        portfolio_id = "host"

        def __init__(self, stop_event):
            self.positions = {}
            self.stop_event = stop_event
            self.clock = None

        def set_clock(self, tm):
            self.clock = tm

        def get_prices(self, assets):
            return {}

    class _Host:
        RISK_OVERLAY = "triple_barrier"
        instances: list[_Host] = []

        @staticmethod
        def default_config() -> dict:
            return {"use_broker_barriers": True}

        def __init__(self, broker, config):
            self.broker = broker
            self.config = config
            self.started = 0
            self.stopped = 0
            self.bars = 0
            type(self).instances.append(self)

        async def init(self):
            pass

        async def on_start(self):
            self.started += 1

        async def on_stop(self):
            self.stopped += 1

        async def on_bar(self, tm):
            self.bars += 1
            self.broker.stop_event.set()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(
        runtime_module.strategy_loader, "load_from_cache", lambda: {"Host": _Host}
    )
    monkeypatch.setattr(runtime_module.asyncio, "sleep", _no_sleep)

    stop_event = threading.Event()
    runtime = StrategyRuntime(
        runtime_id="paper:host",
        mode="paper",
        strategy_name="Host",
        strategy_id="",
        portfolio_id="host",
        account_kind="paper",
        status="running",
        config={"use_broker_barriers": True},
        broker=_FakeBroker(stop_event),
        stop_event=stop_event,
    )

    await StrategyRuntimeManager()._strategy_loop(runtime, "1d", None)

    assert runtime.status == "stopped"
    host = _Host.instances[0]
    assert (host.started, host.stopped, host.bars) == (1, 1, 1)
