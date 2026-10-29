"""Broker-level risk-overlay lifecycle for backtests.

The overlay owns only the exit policy/state for positions already held by a
host strategy.  The broker owns execution and accounting.  Concrete overlays
are supplied by host-owned risk-program adapters; the Community Edition ships
no built-in overlay implementation.
"""

from __future__ import annotations

import datetime
from typing import Any, Protocol


class RiskOverlay(Protocol):
    """Protocol implemented by broker-level backtest risk overlays."""

    async def check_open(self, broker: Any, tm: datetime.datetime) -> None: ...

    async def check_intraday(self, broker: Any, tm: datetime.datetime) -> None: ...

    def sync_positions(self, positions: Any, today: datetime.date) -> None: ...


class CompositeRiskOverlay:
    """Run explicitly selected backtest overlays in priority order."""

    def __init__(
        self,
        overlays: list[Any],
        *,
        allow_same_bar_repurchase: bool = True,
    ) -> None:
        self.overlays = list(overlays)
        self.allow_same_bar_repurchase = allow_same_bar_repurchase

    async def check_open(self, broker: Any, tm: datetime.datetime) -> None:
        for overlay in self.overlays:
            await overlay.check_open(broker, tm)

    async def check_intraday(self, broker: Any, tm: datetime.datetime) -> None:
        for overlay in self.overlays:
            await overlay.check_intraday(broker, tm)

    def sync_positions(self, positions: Any, today: datetime.date) -> None:
        for overlay in self.overlays:
            overlay.sync_positions(positions, today)
