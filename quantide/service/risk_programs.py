"""Generic risk-program extension and adapter boundary.

Risk programs are registered explicitly through adapters.  The full Millionaire
release registers its built-in programs here; the Community Edition removes
those registrations and ships only the generic extension mechanism.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from quantide.core.enums import FrameType
from quantide.core.strategy import BaseStrategy
from quantide.service.risk_overlay import CompositeRiskOverlay

RISK_PROGRAMS_KEY = "risk_programs"
ALLOW_SAME_BAR_REPURCHASE_KEY = "allow_same_bar_repurchase"

RiskCompatibility = Callable[
    [type[BaseStrategy] | None, FrameType | str], bool
]
OverlayFactory = Callable[[dict[str, Any]], Any]
LiveStrategyFactory = Callable[[Any, dict[str, Any]], Any]


@dataclass(frozen=True)
class RiskProgramAdapter:
    """Host-provided adapter for one selectable risk program.

    ``overlay_factory`` adapts the program to the generic backtest overlay
    lifecycle.  ``live_strategy_factory`` adapts it to the unchanged generic
    ``RiskStrategy`` lifecycle.  An adapter may provide either side, but a
    selected program must provide the side required by the caller.
    """

    id: str
    name: str | None = None
    description: str = ""
    default_params: Mapping[str, Any] = field(default_factory=dict)
    compatible: RiskCompatibility | None = None
    overlay_factory: OverlayFactory | None = None
    live_strategy_factory: LiveStrategyFactory | None = None

    @property
    def program_id(self) -> str:
        """Stable public alias for the adapter identifier."""
        return self.id

    def supports(
        self,
        host_strategy_cls: type[BaseStrategy] | None,
        frame_type: FrameType | str,
    ) -> bool:
        if self.compatible is None:
            return True
        return bool(self.compatible(host_strategy_cls, frame_type))

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id.rsplit(".", 1)[-1],
            "description": self.description,
            "params": dict(self.default_params),
            "adapter": self,
        }


class RiskProgramRegistry:
    """Explicit process-local registry for host-owned risk adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, RiskProgramAdapter] = {}

    def register(
        self, adapter: RiskProgramAdapter, *, replace: bool = False
    ) -> RiskProgramAdapter:
        if not isinstance(adapter, RiskProgramAdapter):
            raise TypeError("risk program adapter must be a RiskProgramAdapter")
        program_id = str(adapter.id).strip()
        if not program_id:
            raise ValueError("risk program adapter id cannot be empty")
        if program_id in self._adapters and not replace:
            raise ValueError(f"risk program adapter already registered: {program_id}")
        self._adapters[program_id] = adapter
        return adapter

    def unregister(self, program_id: str) -> RiskProgramAdapter | None:
        return self._adapters.pop(str(program_id), None)

    def get(self, program_id: str) -> RiskProgramAdapter | None:
        return self._adapters.get(str(program_id))

    def clear(self) -> None:
        """Remove registered adapters; useful for host lifecycle and tests."""
        self._adapters.clear()

    def adapters(
        self,
        host_strategy_cls: type[BaseStrategy] | None = None,
        frame_type: FrameType | str = FrameType.DAY,
    ) -> list[RiskProgramAdapter]:
        return [
            adapter
            for adapter in self._adapters.values()
            if adapter.supports(host_strategy_cls, frame_type)
        ]

    def __len__(self) -> int:
        return len(self._adapters)


risk_program_registry = RiskProgramRegistry()


def register_risk_program(
    adapter: RiskProgramAdapter, *, replace: bool = False
) -> RiskProgramAdapter:
    """Register a host-owned risk adapter and return it for decorator use."""
    return risk_program_registry.register(adapter, replace=replace)


def unregister_risk_program(program_id: str) -> RiskProgramAdapter | None:
    """Remove a host-owned adapter, if present."""
    return risk_program_registry.unregister(program_id)


def available_risk_programs(
    host_strategy_cls: type[BaseStrategy] | None = None,
    frame_type: FrameType | str = FrameType.DAY,
) -> list[dict[str, Any]]:
    """List explicitly registered adapters compatible with the host/bar type."""
    return [adapter.metadata() for adapter in risk_program_registry.adapters(host_strategy_cls, frame_type)]


def _available_by_id(
    host_strategy_cls: type[BaseStrategy] | None,
    frame_type: FrameType | str,
) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item
        for item in available_risk_programs(host_strategy_cls, frame_type)
    }


def unavailable_risk_program_ids(
    selected: Any,
    *,
    host_strategy_cls: type[BaseStrategy] | None = None,
    frame_type: FrameType | str = FrameType.DAY,
) -> list[str]:
    """Return persisted IDs that cannot be resolved by the current registry."""
    available = _available_by_id(host_strategy_cls, frame_type)
    values = selected if isinstance(selected, list) else []
    result: list[str] = []
    for item in values:
        program_id = str(item.get("id") if isinstance(item, dict) else item).strip()
        if program_id and program_id not in available and program_id not in result:
            result.append(program_id)
    return result


def normalize_risk_programs(
    selected: Any,
    *,
    host_strategy_cls: type[BaseStrategy] | None = None,
    frame_type: FrameType | str = FrameType.DAY,
) -> list[dict[str, Any]]:
    """Validate and deterministically order explicitly selected adapters."""
    if selected in (None, ""):
        return []
    if isinstance(selected, str):
        try:
            selected = json.loads(selected)
        except json.JSONDecodeError as exc:
            raise ValueError("risk_programs 必须是 JSON 数组") from exc
    if not isinstance(selected, list):
        raise ValueError("risk_programs 必须是数组")

    available = _available_by_id(host_strategy_cls, frame_type)
    normalized: list[tuple[int, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, item in enumerate(selected):
        if isinstance(item, str):
            item = {"id": item, "params": {}}
        if not isinstance(item, dict):
            raise ValueError("risk_programs 项必须是对象")
        program_id = str(item.get("id") or "").strip()
        if program_id not in available:
            raise ValueError(f"不可用的回测风控程序: {program_id or '<empty>'}")
        if program_id in seen:
            raise ValueError(f"回测风控程序重复选择: {program_id}")
        seen.add(program_id)
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"风控程序 {program_id} 的参数必须是对象")
        defaults = available[program_id]["params"]
        effective_params = dict(defaults)
        effective_params.update(params)
        try:
            priority = int(item.get("priority", index))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"风控程序 {program_id} 的优先级无效") from exc
        normalized.append(
            (priority, index, {"id": program_id, "params": effective_params})
        )

    normalized.sort(key=lambda row: (row[0], row[1]))
    return [
        {**item, "priority": priority}
        for priority, (_, _, item) in enumerate(normalized)
    ]


def risk_program_names(
    selected: Any,
    *,
    host_strategy_cls: type[BaseStrategy] | None = None,
    frame_type: FrameType | str = FrameType.DAY,
) -> list[str]:
    """Return names while marking persisted IDs unavailable in this CE host."""
    available = _available_by_id(host_strategy_cls, frame_type)
    names: list[str] = []
    for item in selected or []:
        program_id = str(item.get("id") if isinstance(item, dict) else item)
        metadata = available.get(program_id)
        names.append(
            str(metadata["name"])
            if metadata is not None
            else f"不可用（{program_id}）"
        )
    return names


def build_backtest_risk_overlay(
    strategy_cls: type[BaseStrategy],
    config: dict[str, Any],
    frame_type: FrameType | str,
) -> CompositeRiskOverlay | None:
    """Build an ordered composite from selected adapters, if any."""
    selected = normalize_risk_programs(
        config.get(RISK_PROGRAMS_KEY, []),
        host_strategy_cls=strategy_cls,
        frame_type=frame_type,
    )
    overlays: list[Any] = []
    for item in selected:
        adapter = risk_program_registry.get(str(item["id"]))
        if adapter is None or adapter.overlay_factory is None:
            raise ValueError(f"风控程序缺少回测适配器: {item['id']}")
        overlays.append(adapter.overlay_factory(dict(item["params"])))
    if not overlays:
        return None
    return CompositeRiskOverlay(
        overlays,
        allow_same_bar_repurchase=bool(
            config.get(ALLOW_SAME_BAR_REPURCHASE_KEY, True)
        ),
    )


def build_live_risk_strategies(
    strategy_cls: type[BaseStrategy],
    broker: Any,
    config: dict[str, Any],
    frame_type: FrameType | str = FrameType.DAY,
) -> list[Any]:
    """Instantiate explicitly selected live adapters or legacy declarations."""
    if RISK_PROGRAMS_KEY not in config:
        if getattr(strategy_cls, "RISK_OVERLAY", None) != "triple_barrier":
            return []
        if "use_broker_barriers" in config:
            enabled = bool(config["use_broker_barriers"])
        else:
            defaults = getattr(strategy_cls, "default_config", None)
            enabled = bool(defaults().get("use_broker_barriers", False)) if callable(defaults) else False
        if not enabled:
            return []
        adapter = risk_program_registry.get(TRIPLE_BARRIER_ID)
        if adapter is None or adapter.live_strategy_factory is None:
            return []
        return [adapter.live_strategy_factory(broker, dict(config))]
    selected = normalize_risk_programs(
        config.get(RISK_PROGRAMS_KEY, []),
        host_strategy_cls=strategy_cls,
        frame_type=frame_type,
    )
    strategies: list[Any] = []
    for item in selected:
        adapter = risk_program_registry.get(str(item["id"]))
        if adapter is None or adapter.live_strategy_factory is None:
            raise ValueError(f"风控程序缺少实时适配器: {item['id']}")
        strategies.append(
            adapter.live_strategy_factory(broker, dict(item["params"]))
        )
    return strategies


TRIPLE_BARRIER_ID = "quantide.strategies.triple_barrier_risk.TripleBarrierRiskStrategy"


def _triple_barrier_overlay(params: dict[str, Any]) -> Any:
    from quantide.service.risk_overlay import TripleBarrierOverlay
    from quantide.service.triple_barrier import TripleBarrierConfig

    values = {**params}
    return TripleBarrierOverlay(
        TripleBarrierConfig(
            up_pct=float(values.get("take_profit_pct", values.get("up_pct", 0.15))),
            down_pct=float(values.get("stop_loss_pct", values.get("down_pct", 0.06))),
            horizon=int(values.get("horizon", 3)),
            sell_fraction=float(values.get("sell_fraction", 1.0)),
            slippage=float(values.get("slippage_pct", values.get("slippage", 0.001))),
        )
    )


def _triple_barrier_live(broker: Any, params: dict[str, Any]) -> Any:
    from quantide.strategies.triple_barrier_risk import TripleBarrierRiskStrategy

    return TripleBarrierRiskStrategy(broker, dict(params))


def _supports_daily_or_30m(
    host_strategy_cls: type[BaseStrategy] | None,
    frame_type: FrameType | str,
) -> bool:
    del host_strategy_cls
    interval = frame_type.value if isinstance(frame_type, FrameType) else str(frame_type)
    return interval in {FrameType.DAY.value, "1d", FrameType.MIN30.value}


register_risk_program(
    RiskProgramAdapter(
        id=TRIPLE_BARRIER_ID,
        name="三重障碍",
        description="按成本设置止盈、止损和持有期限，遵守 A 股 T+1。",
        default_params={
            "take_profit_pct": 0.15,
            "stop_loss_pct": 0.06,
            "horizon": 3,
            "sell_fraction": 1.0,
            "slippage_pct": 0.001,
        },
        compatible=_supports_daily_or_30m,
        overlay_factory=_triple_barrier_overlay,
        live_strategy_factory=_triple_barrier_live,
    )
)
