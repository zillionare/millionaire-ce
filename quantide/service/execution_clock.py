"""交易时钟：交易日判断 + 集合竞价窗口。

A 股收盘集合竞价为 **14:57-15:00**（3 分钟）。日频策略的执行时序：

.. code-block:: text

    14:57:00  行情源开始给出集合竞价价（price/high/low = 竞价价）
    14:57-14:59  策略用「截至当前的实时价」计算信号（预算 2 分钟）
    14:59:00  以「竞价价 × (1 + epsilon)」下单
    15:00:00  集合竞价撮合成交

若 14:59 前没算完，**继续算**（不回退到次日开盘）——两分钟预算只是目标，
不是硬性中断。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

#: 收盘集合竞价开始时间
AUCTION_START = datetime.time(14, 57)
#: 收盘集合竞价结束时间
AUCTION_END = datetime.time(15, 0)
#: 集合竞价下单的截止时间（留 1 分钟下单窗口）
AUCTION_ORDER_DEADLINE = datetime.time(14, 59)
#: 连续竞价结束时间
CONTINUOUS_END = datetime.time(15, 0)


@dataclass
class ExecutionPhase:
    """当前所处的执行阶段。"""

    name: str
    """阶段名：pre_open / auction / continuous / post_close / closed"""

    is_auction: bool = False
    """是否处于集合竞价窗口"""

    order_allowed: bool = False
    """是否允许下单"""

    def __str__(self) -> str:
        return self.name


class ExecutionClock:
    """交易时钟（基于交易日历判断可交易状态）。"""

    def __init__(self, calendar: Any = None) -> None:
        """初始化时钟。

        Args:
            calendar: 日历对象；None 表示每次现取全局日历。
        """
        self._calendar = calendar
        self._trading_days: set[datetime.date] | None = None

    @property
    def calendar(self) -> Any:
        """日历对象。"""
        if self._calendar is None:
            from quantide.data.models.calendar import calendar

            self._calendar = calendar
        return self._calendar

    # ------------------------------------------------------------------
    # 交易日
    # ------------------------------------------------------------------
    def is_trading_day(self, day: datetime.date) -> bool:
        """判断是否交易日（交易日集合缓存，避免每次重建列表）。"""
        if self._trading_days is None:
            self._trading_days = self._load_trading_days()
        if not self._trading_days:
            return True
        return day in self._trading_days

    def _load_trading_days(self) -> set[datetime.date]:
        """加载交易日集合。"""
        try:
            frames = self.calendar.day_frames
            if frames is None:
                return set()
            return {d for d in frames.to_pylist() if isinstance(d, datetime.date)}
        except Exception:  # pragma: no cover - 日历不可用
            return set()

    def previous_trading_day(self, day: datetime.date) -> datetime.date | None:
        """上一个交易日。"""
        try:
            from quantide.core.enums import FrameType

            shifted = self.calendar.shift(day, -1, FrameType.DAY)
            return shifted if isinstance(shifted, datetime.date) else None
        except Exception:  # pragma: no cover - 日历不可用
            return None

    # ------------------------------------------------------------------
    # 阶段
    # ------------------------------------------------------------------
    def phase(self, now: datetime.datetime) -> ExecutionPhase:
        """返回当前所处阶段。

        Args:
            now: 当前时间。

        Returns:
            ExecutionPhase: 阶段信息。
        """
        if not self.is_trading_day(now.date()):
            return ExecutionPhase("closed", is_auction=False, order_allowed=False)
        clock = now.time()
        if clock < datetime.time(9, 30):
            return ExecutionPhase("pre_open", is_auction=False, order_allowed=False)
        if clock < AUCTION_START:
            return ExecutionPhase("continuous", is_auction=False, order_allowed=True)
        if clock < AUCTION_END:
            return ExecutionPhase(
                "auction", is_auction=True, order_allowed=clock <= AUCTION_ORDER_DEADLINE
            )
        if clock < CONTINUOUS_END:
            return ExecutionPhase("continuous", is_auction=False, order_allowed=True)
        return ExecutionPhase("post_close", is_auction=False, order_allowed=False)

    def in_auction(self, now: datetime.datetime) -> bool:
        """是否处于集合竞价窗口。"""
        return self.phase(now).is_auction

    def auction_price_limit(self, price: float, epsilon: float = 0.001) -> float:
        """集合竞价的下单限价（竞价价 × (1 + epsilon)）。"""
        return round(price * (1.0 + float(epsilon)), 4)

    def seconds_to_auction_end(self, now: datetime.datetime) -> float:
        """距离集合竞价结束的秒数（用于进度提示）。"""
        end = datetime.datetime.combine(now.date(), AUCTION_END)
        return max(0.0, (end - now).total_seconds())


#: 全局单例
execution_clock = ExecutionClock()
