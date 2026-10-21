"""Live/paper triple-barrier risk strategy.

The decision rules are shared with the backtest overlay via
``quantide.service.triple_barrier``.  This strategy only executes them against
the latest price / tick in paper/live mode.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

from quantide.core.enums import FrameType
from quantide.core.strategy import RiskStrategy
from quantide.data.models.calendar import calendar
from quantide.service.triple_barrier import (
    ExitPlan,
    TripleBarrierConfig,
    plan_tick_exit,
)


class TripleBarrierRiskStrategy(RiskStrategy):
    """Cost-based stop/take-profit/time exit for a host strategy.

    T0 (the day a position is first observed) is never sold because A-share
    T+1 applies.  If a barrier is hit on T0, the exit is deferred to the first
    check on the next trading day, mirroring the backtest overlay.
    """

    _MORNING = (datetime.time(9, 30), datetime.time(11, 30))
    _AFTERNOON = (datetime.time(13, 0), datetime.time(15, 0))

    @staticmethod
    def default_config() -> dict[str, Any]:
        return {
            "up_pct": 0.15,
            "down_pct": 0.06,
            "horizon": 3,
            "sell_fraction": 1.0,
        }

    def __init__(self, broker: Any, config: dict[str, Any]):
        super().__init__(broker, config)
        cfg = {**self.default_config(), **(config or {})}
        # Host strategy config uses take_profit_pct / stop_loss_pct.
        self.up_pct = float(cfg.get("take_profit_pct", cfg.get("up_pct", 0.15)))
        self.down_pct = float(cfg.get("stop_loss_pct", cfg.get("down_pct", 0.06)))
        self.horizon = max(1, int(cfg.get("horizon", 3)))
        self.sell_fraction = min(
            1.0, max(0.0, float(cfg.get("sell_fraction", 1.0)))
        )
        self.policy = TripleBarrierConfig(
            up_pct=self.up_pct,
            down_pct=self.down_pct,
            horizon=self.horizon,
            sell_fraction=self.sell_fraction,
            slippage=0.0,
        ).normalized()
        self._entry_dates: dict[str, datetime.date] = {}
        self._holding_days: dict[str, int] = {}
        self._pending: dict[str, ExitPlan] = {}
        self._pending_logged_on: dict[str, datetime.date] = {}
        self._triggered_today: set[str] = set()
        self._last_day: datetime.date | None = None

    @staticmethod
    def _as_mapping(positions: Any) -> dict[str, Any]:
        if isinstance(positions, Mapping):
            return dict(positions)
        return {
            str(getattr(pos, "asset", "")): pos
            for pos in positions or []
            if str(getattr(pos, "asset", ""))
        }

    def _last_price(self, asset: str, pos: Any) -> float:
        getter = getattr(self.broker, "get_prices", None)
        if callable(getter):
            try:
                prices = getter([asset])
                value = float(prices.get(asset, 0.0) or 0.0)
                if value > 0:
                    return value
            except Exception:
                pass
        shares = float(getattr(pos, "shares", 0) or 0)
        mv = float(getattr(pos, "mv", 0) or 0)
        if shares > 0 and mv > 0:
            return mv / shares
        return float(getattr(pos, "price", 0) or 0)

    @staticmethod
    def _held_trading_days(entry: datetime.date, today: datetime.date) -> int:
        """Count elapsed trading days.

        Falls back to calendar days if the trading calendar is unavailable
        (e.g. isolated unit tests).
        """
        if today <= entry:
            return 0
        try:
            frames = calendar.get_frames(entry, today, FrameType.DAY)
            return max(0, len(frames) - 1)
        except Exception:
            return (today - entry).days

    @classmethod
    def _in_trading_session(cls, tm: datetime.datetime) -> bool:
        t = tm.time()
        return (
            cls._MORNING[0] <= t <= cls._MORNING[1]
            or cls._AFTERNOON[0] <= t <= cls._AFTERNOON[1]
        )

    @staticmethod
    def _is_trade_day(day: datetime.date) -> bool:
        try:
            return bool(calendar.is_trade_day(day))
        except Exception:
            return True

    def _advance_day(self, today: datetime.date, positions: dict[str, Any]) -> None:
        if self._last_day == today:
            return
        self._triggered_today.clear()
        for asset in positions:
            entry = self._entry_dates.get(asset)
            if entry is not None:
                self._holding_days[asset] = self._held_trading_days(entry, today)
        self._last_day = today

    async def on_check(self, positions: Any, tm: datetime.datetime) -> None:
        current = self._as_mapping(positions)
        today = tm.date()
        self._advance_day(today, current)
        can_trade = self._in_trading_session(tm) and self._is_trade_day(today)

        for asset in list(self._entry_dates):
            if asset not in current:
                self._entry_dates.pop(asset, None)
                self._holding_days.pop(asset, None)
                self._pending.pop(asset, None)
                self._pending_logged_on.pop(asset, None)
                self._triggered_today.discard(asset)

        for asset, pos in current.items():
            shares = float(getattr(pos, "shares", 0) or 0)
            avail = float(getattr(pos, "avail", shares) or 0)
            cost = float(getattr(pos, "price", 0) or 0)
            if shares <= 0 or cost <= 0:
                continue

            if asset not in self._entry_dates:
                self._entry_dates[asset] = today
                self._holding_days[asset] = 0

            if asset in self._triggered_today:
                continue

            is_entry_day = self._entry_dates.get(asset) == today
            held_days = self._holding_days.get(asset, 0)
            price = self._last_price(asset, pos)
            plan = plan_tick_exit(
                last_price=price,
                cost=cost,
                config=self.policy,
                held_days=held_days,
                pending=self._pending.get(asset),
                is_entry_day=is_entry_day,
            )
            if plan.action == "pending":
                self._pending[asset] = plan
                if can_trade and self._pending_logged_on.get(asset) != today:
                    self._pending_logged_on[asset] = today
                    self.log(
                        f"[风控挂起] {asset} T+1不可卖，{plan.reason} "
                        f"触发价={plan.trigger_price:.2f}",
                        tm=tm,
                    )
                continue
            if plan.action != "sell" or not can_trade or avail <= 0:
                continue

            sell_shares = avail
            if self.sell_fraction < 1.0:
                sell_shares = int(avail * self.sell_fraction / 100) * 100
            if sell_shares <= 0:
                continue

            try:
                await self.sell_host_position(asset, sell_shares, reason=plan.reason)
            except Exception as exc:
                self.log(
                    f"[风控卖出跳过] {asset} 无法成交: {exc}",
                    level="WARNING",
                    tm=tm,
                )
                continue

            self._triggered_today.add(asset)
            self._pending.pop(asset, None)
            self._pending_logged_on.pop(asset, None)
            remaining = shares - sell_shares
            if remaining <= 0:
                self._entry_dates.pop(asset, None)
                self._holding_days.pop(asset, None)
            self.log(
                f"[风控卖出] {asset} 数量={sell_shares} 原因={plan.reason} "
                f"触发价={plan.trigger_price:.2f}",
                tm=tm,
            )
