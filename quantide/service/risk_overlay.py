"""Broker-level triple-barrier risk overlay for daily backtests.

The overlay owns only the exit *policy/state* for positions already held by a
host strategy.  The broker owns execution and accounting.  The decision rules
are shared with the live risk strategy via ``triple_barrier``.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any, Protocol

from quantide.service.triple_barrier import (
    ExitPlan,
    TripleBarrierConfig,
    plan_intraday_exit,
    plan_open_exit,
)


class RiskOverlay(Protocol):
    """Protocol implemented by broker-level backtest risk overlays."""

    async def check_open(self, broker: Any, tm: datetime.datetime) -> None: ...

    async def check_intraday(self, broker: Any, tm: datetime.datetime) -> None: ...

    def sync_positions(self, positions: Any, today: datetime.date) -> None: ...


class TripleBarrierOverlay:
    """Daily-bar risk overlay.

    Timeline (T0 = buy day):

    - T0: never sell.  If T0's low/high hits a barrier, remember it and sell at
      T1 open (A-share T+1).
    - Tn (n >= 1): at Tn open, sell on a gap through the barrier or on the time
      barrier.  After the host's ``on_bar`` has run, inspect Tn's low/high and
      sell at the barrier price if it was touched intraday.
    """

    def __init__(self, config: TripleBarrierConfig | None = None) -> None:
        self.config = (config or TripleBarrierConfig()).normalized()
        self._holding_days: dict[str, int] = {}
        self._last_seen_date: dict[str, datetime.date] = {}
        self._entry_dates: dict[str, datetime.date] = {}
        self._pending: dict[str, ExitPlan] = {}

    def reset(self) -> None:
        self._holding_days.clear()
        self._last_seen_date.clear()
        self._entry_dates.clear()
        self._pending.clear()

    @staticmethod
    def _as_mapping(positions: Any) -> dict[str, Any]:
        if isinstance(positions, Mapping):
            return dict(positions)
        return {
            str(getattr(pos, "asset", "")): pos
            for pos in positions or []
            if str(getattr(pos, "asset", ""))
        }

    def sync_positions(self, positions: Any, today: datetime.date) -> None:
        held = {
            asset
            for asset, pos in self._as_mapping(positions).items()
            if float(getattr(pos, "shares", 0) or 0) > 0
        }
        for asset in list(self._holding_days):
            if asset not in held:
                self._holding_days.pop(asset, None)
                self._last_seen_date.pop(asset, None)
                self._entry_dates.pop(asset, None)
                self._pending.pop(asset, None)
        for asset in held:
            last = self._last_seen_date.get(asset)
            if last is None:
                self._entry_dates[asset] = today
                self._holding_days[asset] = 0
            elif today > last:
                self._holding_days[asset] = self._holding_days.get(asset, 0) + 1
            self._last_seen_date[asset] = today

    def _bar(
        self,
        broker: Any,
        asset: str,
        tm: datetime.datetime,
        interval: str | None = None,
    ) -> dict[str, Any] | None:
        if interval == "30m":
            getter = getattr(broker, "get_bar_for_time", None)
            if callable(getter):
                return getter(asset, tm)
        getter = getattr(broker, "get_bar_for_date", None)
        if callable(getter):
            return getter(asset, tm.date())
        return None

    async def check_open(self, broker: Any, tm: datetime.datetime) -> None:
        """Check gap/time exits before the host's ``on_bar`` runs."""
        today = tm.date()
        interval = getattr(broker, "interval", None)
        positions = self._as_mapping(broker.positions)
        self.sync_positions(positions, today)

        for asset, pos in positions.items():
            shares = float(getattr(pos, "shares", 0) or 0)
            avail = float(getattr(pos, "avail", shares) or 0)
            cost = float(getattr(pos, "price", 0) or 0)
            if shares <= 0 or avail <= 0 or cost <= 0:
                continue
            bar = self._bar(broker, asset, tm, interval)
            if not bar:
                continue
            plan = plan_open_exit(
                open_price=float(bar.get("open", 0) or 0),
                cost=cost,
                config=self.config,
                held_days=self._holding_days.get(asset, 0),
                pending=self._pending.get(asset),
            )
            if plan.action == "sell":
                await self._execute(broker, asset, avail, plan, tm, today, at_open=True)

    async def check_intraday(self, broker: Any, tm: datetime.datetime) -> None:
        """Check intraday barrier touches after ``on_bar`` has run."""
        today = tm.date()
        interval = getattr(broker, "interval", None)
        positions = self._as_mapping(broker.positions)

        for asset, pos in positions.items():
            shares = float(getattr(pos, "shares", 0) or 0)
            avail = float(getattr(pos, "avail", shares) or 0)
            cost = float(getattr(pos, "price", 0) or 0)
            if shares <= 0 or cost <= 0:
                continue
            bar = self._bar(broker, asset, tm, interval)
            if not bar:
                continue
            is_entry_day = self._entry_dates.get(asset) == today
            plan = plan_intraday_exit(
                open_price=float(bar.get("open", 0) or 0),
                high=float(bar.get("high", 0) or 0),
                low=float(bar.get("low", 0) or 0),
                close=float(bar.get("close", 0) or 0),
                cost=cost,
                config=self.config,
                is_entry_day=is_entry_day,
                pending=self._pending.get(asset),
            )
            if plan.action == "pending":
                self._pending[asset] = plan
                self._log(
                    broker,
                    "INFO",
                    f"[风控挂起] {asset} T+1不可卖，{plan.reason} 触发价={plan.trigger_price:.2f}",
                    tm,
                )
                continue
            if plan.action != "sell" or avail <= 0:
                continue
            await self._execute(broker, asset, avail, plan, tm, today, at_open=False)

    async def _execute(
        self,
        broker: Any,
        asset: str,
        avail: float,
        plan: ExitPlan,
        tm: datetime.datetime,
        today: datetime.date,
        *,
        at_open: bool,
    ) -> None:
        sell_shares = avail
        if self.config.sell_fraction < 1.0:
            sell_shares = int(avail * self.config.sell_fraction / 100) * 100
        if sell_shares <= 0:
            return

        if at_open:
            execution_tm = tm
        else:
            # 正常日线宿主以 09:30 作为 bar 时间，盘中条件单统一记 14:55；
            # cheat_on_close=True 时 bar 时间可能是 14:57，订单时间不能早于
            # broker 当前时钟，否则 set_clock 会抛 ClockRewind 并吞掉卖单。
            intraday_tm = tm.replace(hour=14, minute=55, second=0, microsecond=0)
            execution_tm = tm if tm >= intraday_tm else intraday_tm
        price = plan.fill_price
        try:
            execute = getattr(broker, "execute_exit", None)
            if callable(execute):
                await execute(
                    asset,
                    sell_shares,
                    price=price,
                    order_time=execution_tm,
                    reason=plan.reason,
                )
            else:
                await broker.sell(
                    asset,
                    sell_shares,
                    price=price,
                    order_time=execution_tm,
                )
        except Exception as exc:
            self._log(
                broker,
                "WARNING",
                f"[风控卖出跳过] {asset} 无法成交: {exc}",
                execution_tm,
            )
            return

        remaining_pos = self._as_mapping(broker.positions).get(asset)
        remaining = float(getattr(remaining_pos, "shares", 0) or 0)
        if remaining <= 0:
            self._holding_days.pop(asset, None)
            self._last_seen_date.pop(asset, None)
            self._entry_dates.pop(asset, None)
            self._pending.pop(asset, None)
        self._log(
            broker,
            "INFO",
            f"[风控卖出] {asset} 数量={sell_shares} 原因={plan.reason} "
            f"触发价={plan.trigger_price:.2f} 成交价={price:.2f} "
            f"执行日={execution_tm.date()}",
            execution_tm,
        )

    @staticmethod
    def _log(broker: Any, level: str, message: str, tm: datetime.datetime) -> None:
        log = getattr(broker, "_log", None)
        if callable(log):
            log(level, message, dt=tm)


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
