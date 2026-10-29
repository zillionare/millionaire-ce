"""Wiring tests for generic live risk-program adapters."""

from __future__ import annotations

import threading

import pytest

from quantide.service.risk_programs import (
    RiskProgramAdapter,
    register_risk_program,
    risk_program_registry,
)
from quantide.service.strategy_runtime import StrategyRuntimeManager


class _LiveRisk:
    def __init__(self, broker, params):
        self.broker = broker
        self.params = params

    async def init(self):
        return None

    async def on_start(self):
        return None

    async def on_stop(self):
        return None

    async def on_day_open(self, tm):
        return None

    async def on_check(self, positions, tm):
        return None


class _Broker:
    portfolio_id = "host"


@pytest.fixture
def registered_program():
    adapter = RiskProgramAdapter(
        id="host.declared.Risk",
        live_strategy_factory=_LiveRisk,
    )
    register_risk_program(adapter, replace=True)
    try:
        yield adapter
    finally:
        risk_program_registry.unregister(adapter.id)


def test_live_risk_strategy_is_attached_for_declared_overlay(registered_program):
    class Host:
        RISK_OVERLAY = registered_program.id

    manager = StrategyRuntimeManager()
    risk = manager._build_live_risk_strategy(Host, _Broker(), {})

    assert isinstance(risk, _LiveRisk)


def test_live_risk_strategy_can_be_disabled(registered_program):
    class Host:
        RISK_OVERLAY = registered_program.id

    manager = StrategyRuntimeManager()
    risk = manager._build_live_risk_strategy(
        Host, _Broker(), {"use_broker_barriers": False}
    )

    assert risk is None


@pytest.mark.asyncio
async def test_strategy_loop_starts_and_stops_without_lifecycle_type_error(
    monkeypatch, registered_program
):
    """on_start/on_stop 只接受 self；runtime 不能传 timestamp (B2)."""
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
        RISK_OVERLAY = registered_program.id
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
