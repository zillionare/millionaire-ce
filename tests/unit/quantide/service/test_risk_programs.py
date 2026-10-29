"""CE tests for the explicit, host-owned risk-program boundary."""

from __future__ import annotations

import pytest

from quantide.service.risk_programs import (
    ALLOW_SAME_BAR_REPURCHASE_KEY,
    RISK_PROGRAMS_KEY,
    RiskProgramAdapter,
    available_risk_programs,
    build_backtest_risk_overlay,
    build_live_risk_strategies,
    normalize_risk_programs,
    register_risk_program,
    risk_program_registry,
)


class _Host:
    """Minimal host with no private risk declaration."""

    BAR_INTERVAL = "1d"


class _Overlay:
    async def check_open(self, broker, tm):
        return None

    async def check_intraday(self, broker, tm):
        return None

    def sync_positions(self, positions, today):
        return None


class _LiveRisk:
    def __init__(self, broker, params):
        self.broker = broker
        self.params = params


@pytest.fixture
def clean_registry():
    previous = dict(risk_program_registry._adapters)
    risk_program_registry.clear()
    yield
    risk_program_registry.clear()
    risk_program_registry._adapters.update(previous)


def _adapter(program_id: str, *, priority_default: int = 1) -> RiskProgramAdapter:
    return RiskProgramAdapter(
        id=program_id,
        name=program_id.rsplit(".", 1)[-1],
        description="host adapter",
        default_params={"priority_default": priority_default},
        compatible=lambda host, frame: str(getattr(host, "BAR_INTERVAL", frame))
        == str(frame),
        overlay_factory=lambda params: _Overlay(),
        live_strategy_factory=_LiveRisk,
    )


def test_no_registered_risk_programs_are_a_valid_empty_selection(clean_registry):
    assert available_risk_programs(_Host, "1d") == []
    assert normalize_risk_programs([], host_strategy_cls=_Host, frame_type="1d") == []
    assert build_backtest_risk_overlay(_Host, {}, "1d") is None
    assert build_live_risk_strategies(_Host, object(), {}, frame_type="1d") == []


def test_old_private_id_is_unavailable_instead_of_substituted(clean_registry):
    with pytest.raises(ValueError, match="不可用"):
        normalize_risk_programs(
            [
                {
                    "id": "quantide.strategies.triple_barrier_risk.TripleBarrierRiskStrategy",
                    "params": {},
                }
            ],
            host_strategy_cls=_Host,
            frame_type="1d",
        )


def test_generic_adapter_registration_normalizes_and_builds_ordered_composite(
    clean_registry,
):
    first = _adapter("private.learner.FirstRisk", priority_default=1)
    second = _adapter("private.learner.SecondRisk", priority_default=2)
    register_risk_program(first)
    register_risk_program(second)

    selected = normalize_risk_programs(
        [
            {"id": second.id, "priority": 8},
            {"id": first.id, "priority": 2},
        ],
        host_strategy_cls=_Host,
        frame_type="1d",
    )
    assert [item["id"] for item in selected] == [first.id, second.id]
    overlay = build_backtest_risk_overlay(
        _Host,
        {
            RISK_PROGRAMS_KEY: selected,
            ALLOW_SAME_BAR_REPURCHASE_KEY: False,
        },
        "1d",
    )
    assert overlay is not None
    assert [type(item) for item in overlay.overlays] == [_Overlay, _Overlay]
    assert overlay.allow_same_bar_repurchase is False

    live = build_live_risk_strategies(
        _Host,
        object(),
        {RISK_PROGRAMS_KEY: selected},
        frame_type="1d",
    )
    assert [item.params["priority_default"] for item in live] == [1, 2]


def test_host_without_risk_overlay_declaration_can_use_empty_selection(clean_registry):
    class HostWithoutPrivateDeclaration:
        BAR_INTERVAL = "1d"

    assert not hasattr(HostWithoutPrivateDeclaration, "RISK_OVERLAY")
    assert normalize_risk_programs(
        [], host_strategy_cls=HostWithoutPrivateDeclaration, frame_type="1d"
    ) == []


def test_host_risk_overlay_declaration_resolves_registered_adapter(clean_registry):
    adapter = _adapter("host.declared.Risk")
    register_risk_program(adapter)

    class DeclaringHost:
        BAR_INTERVAL = "1d"
        RISK_OVERLAY = adapter.id

    live = build_live_risk_strategies(DeclaringHost, object(), {}, frame_type="1d")
    assert len(live) == 1
    assert isinstance(live[0], _LiveRisk)
    assert live[0].params == {}

    assert (
        build_live_risk_strategies(
            DeclaringHost,
            object(),
            {"use_broker_barriers": False},
            frame_type="1d",
        )
        == []
    )


def test_undeclared_or_unregistered_overlay_does_not_invent_a_program(clean_registry):
    class UndeclaredHost:
        BAR_INTERVAL = "1d"

    class UnregisteredHost:
        BAR_INTERVAL = "1d"
        RISK_OVERLAY = "host.missing.Risk"

    assert build_live_risk_strategies(UndeclaredHost, object(), {}) == []
    assert build_live_risk_strategies(UnregisteredHost, object(), {}) == []
