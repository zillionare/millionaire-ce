"""FR-360 Triple Barrier 公式实施 (F-TB-1 ~ F-TB-5).

按 spec-trading.md §FR-360 + story §1.9:

单位说明:
- up_threshold / down_threshold: 百分点 (5.0 表示 5%)
- excess_return: 小数比率 (0.05 表示 5%)
- P_sell: 卖出价
- Close_n: T_n 日收盘价 (N=0 即当日收盘)
- high / low: 当日最高 / 最低价

公式:
- F-TB-1: up 屏障触发 excess_return = -1 * (up_threshold / 100)
- F-TB-2: down 屏障触发 excess_return = +1 * (down_threshold / 100)
- F-TB-3: expire excess_return = P_sell / Close_n - 1
- F-TB-4: up 屏障触发判断 high >= P_sell * (1 + up_threshold / 100)
- F-TB-5: down 屏障触发判断 low <= P_sell * (1 - down_threshold / 100)

一日内同时触达上下界: 以开盘价更接近者为准 (FR-013 / story §1.9).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class BarrierHit(StrEnum):
    """屏障触发类型."""

    UP = "up"
    DOWN = "down"
    EXPIRE = "expire"


@dataclass(frozen=True)
class BarrierConfig:
    """屏障配置."""

    up_threshold: float  # 百分点, 如 5.0 = 5%
    down_threshold: float  # 百分点, 如 5.0 = 5%
    n: int = 0  # 观察期 N 日, 0 = 当日


@dataclass(frozen=True)
class BarEvaluation:
    """单日屏障评估结果."""

    barrier_hit: BarrierHit
    excess_return: float
    trigger_price: float


def check_up_barrier(high: float, p_sell: float, up_threshold: float) -> bool:
    """F-TB-4: up 屏障触发判断.

    Args:
        high: 当日最高价
        p_sell: 卖出价
        up_threshold: 上阈值 (百分点, 如 5.0)

    Returns:
        True 当 high >= P_sell * (1 + up_threshold / 100)
    """
    return high >= p_sell * (1 + up_threshold / 100)


def check_down_barrier(low: float, p_sell: float, down_threshold: float) -> bool:
    """F-TB-5: down 屏障触发判断.

    Args:
        low: 当日最低价
        p_sell: 卖出价
        down_threshold: 下阈值 (百分点, 如 5.0)

    Returns:
        True 当 low <= P_sell * (1 - down_threshold / 100)
    """
    return low <= p_sell * (1 - down_threshold / 100)


def excess_return_up(up_threshold: float) -> float:
    """F-TB-1: up 屏障触发的超额收益.

    Args:
        up_threshold: 上阈值 (百分点)

    Returns:
        excess_return = -1 * (up_threshold / 100) (小数)
    """
    return -1 * (up_threshold / 100)


def excess_return_down(down_threshold: float) -> float:
    """F-TB-2: down 屏障触发的超额收益.

    Args:
        down_threshold: 下阈值 (百分点)

    Returns:
        excess_return = +1 * (down_threshold / 100) (小数)
    """
    return down_threshold / 100


def excess_return_expire(p_sell: float, close_n: float) -> float:
    """F-TB-3: 观察期到但未触发的超额收益.

    Args:
        p_sell: 卖出价
        close_n: T_n 日收盘价

    Returns:
        excess_return = P_sell / Close_n - 1
        收盘价 < 卖出价 → 正收益 (风控避开下跌)
        收盘价 > 卖出价 → 负收益 (错过上涨)
    """
    if close_n <= 0:
        raise ValueError(f"close_n must be positive, got {close_n}")
    return p_sell / close_n - 1


def evaluate_day(
    high: float,
    low: float,
    open_: float,
    close: float,
    p_sell: float,
    up_threshold: float,
    down_threshold: float,
) -> BarEvaluation:
    """评估单日屏障触发 (含同日触达上下界判断).

    一日内同时触达上下界: 以开盘价更接近者为准 (FR-013 / story §1.9).
    - up 触发价: P_sell * (1 + up_threshold / 100)
    - down 触发价: P_sell * (1 - down_threshold / 100)
    - 开盘价离 up 触发价近 → up; 离 down 触发价近 → down; 等距时优先 down (保守)

    Args:
        high: 当日最高价
        low: 当日最低价
        open_: 当日开盘价
        close: 当日收盘价
        p_sell: 卖出价
        up_threshold: 上阈值 (百分点)
        down_threshold: 下阈值 (百分点)

    Returns:
        BarEvaluation: barrier_hit + excess_return + trigger_price
        当日无触发 (high < up 且 low > down) 时返回 expire, 用当日收盘价
    """
    up_triggered = check_up_barrier(high, p_sell, up_threshold)
    down_triggered = check_down_barrier(low, p_sell, down_threshold)

    if up_triggered and down_triggered:
        up_price = p_sell * (1 + up_threshold / 100)
        down_price = p_sell * (1 - down_threshold / 100)
        up_dist = abs(open_ - up_price)
        down_dist = abs(open_ - down_price)
        if down_dist < up_dist:
            return BarEvaluation(
                barrier_hit=BarrierHit.DOWN,
                excess_return=excess_return_down(down_threshold),
                trigger_price=down_price,
            )
        elif up_dist < down_dist:
            return BarEvaluation(
                barrier_hit=BarrierHit.UP,
                excess_return=excess_return_up(up_threshold),
                trigger_price=up_price,
            )
        else:
            return BarEvaluation(
                barrier_hit=BarrierHit.DOWN,
                excess_return=excess_return_down(down_threshold),
                trigger_price=down_price,
            )

    if up_triggered:
        return BarEvaluation(
            barrier_hit=BarrierHit.UP,
            excess_return=excess_return_up(up_threshold),
            trigger_price=p_sell * (1 + up_threshold / 100),
        )

    if down_triggered:
        return BarEvaluation(
            barrier_hit=BarrierHit.DOWN,
            excess_return=excess_return_down(down_threshold),
            trigger_price=p_sell * (1 - down_threshold / 100),
        )

    return BarEvaluation(
        barrier_hit=BarrierHit.EXPIRE,
        excess_return=excess_return_expire(p_sell, close),
        trigger_price=close,
    )



@dataclass(frozen=True)
class TripleBarrierConfig:
    """统一的三重障碍风控配置（回测与实盘共用）."""

    up_pct: float = 0.15
    down_pct: float = 0.06
    horizon: int = 3
    sell_fraction: float = 1.0
    slippage: float = 0.001

    def normalized(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            up_pct=max(0.0, float(self.up_pct)),
            down_pct=max(0.0, float(self.down_pct)),
            horizon=max(1, int(self.horizon)),
            sell_fraction=min(1.0, max(0.0, float(self.sell_fraction))),
            slippage=min(0.5, max(0.0, float(self.slippage))),
        )


@dataclass(frozen=True)
class ExitPlan:
    """一次退出决策；执行层负责按 fill_price / 市价成交."""

    action: Literal["none", "sell", "pending"] = "none"
    reason: str = ""
    trigger_price: float = 0.0
    fill_price: float = 0.0
    barrier: Literal["up", "down", "time", "pending"] = "pending"
    at_open: bool = False


def _none_plan() -> ExitPlan:
    return ExitPlan()


def _sell_plan(
    *,
    reason: str,
    trigger_price: float,
    fill_price: float,
    barrier: Literal["up", "down", "time", "pending"],
    at_open: bool = False,
) -> ExitPlan:
    return ExitPlan(
        action="sell",
        reason=reason,
        trigger_price=max(0.0, float(trigger_price)),
        fill_price=max(0.0, float(fill_price)),
        barrier=barrier,
        at_open=at_open,
    )


def _pending_plan(
    *,
    reason: str,
    trigger_price: float,
    barrier: Literal["up", "down"],
) -> ExitPlan:
    return ExitPlan(
        action="pending",
        reason=reason,
        trigger_price=max(0.0, float(trigger_price)),
        barrier=barrier,
    )


def _stop_reason(config: TripleBarrierConfig) -> str:
    return f"触发三重障碍下轨止损(-{config.down_pct:.0%})"


def _take_profit_reason(config: TripleBarrierConfig) -> str:
    return f"触发三重障碍上轨止盈(+{config.up_pct:.0%})"


def plan_open_exit(
    *,
    open_price: float,
    cost: float,
    config: TripleBarrierConfig,
    held_days: int = 0,
    pending: ExitPlan | None = None,
) -> ExitPlan:
    """T 日开盘时的退出决策。

    - T0 触发的 barrier 只能 T+1 开盘补卖；
    - 开盘跳空越过 barrier 时，按开盘价成交；
    - 时间屏障在开盘卖出。
    """
    cfg = config.normalized()
    if cost <= 0 or open_price <= 0:
        return _none_plan()

    fill_open = open_price * (1.0 - cfg.slippage)

    if pending is not None and pending.action == "pending":
        return _sell_plan(
            reason=f"{pending.reason}(T+1开盘补卖)",
            trigger_price=pending.trigger_price,
            fill_price=fill_open,
            barrier=pending.barrier,
            at_open=True,
        )

    if held_days >= cfg.horizon:
        return _sell_plan(
            reason=f"触发三重障碍时间到期(持有{held_days}日)",
            trigger_price=open_price,
            fill_price=fill_open,
            barrier="time",
            at_open=True,
        )

    down_price = cost * (1.0 - cfg.down_pct)
    up_price = cost * (1.0 + cfg.up_pct)
    if open_price <= down_price:
        return _sell_plan(
            reason=_stop_reason(cfg),
            trigger_price=down_price,
            fill_price=fill_open,
            barrier="down",
            at_open=True,
        )
    if open_price >= up_price:
        return _sell_plan(
            reason=_take_profit_reason(cfg),
            trigger_price=up_price,
            fill_price=fill_open,
            barrier="up",
            at_open=True,
        )
    return _none_plan()


def plan_intraday_exit(
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    cost: float,
    config: TripleBarrierConfig,
    is_entry_day: bool = False,
    pending: ExitPlan | None = None,
) -> ExitPlan:
    """T 日盘中/收盘后的退出决策（日线回测用 OHLC 近似）.

    同日上下轨都触发时，沿用 FR-013：开盘价更接近者优先，等距 down。
    T0 触发的 barrier 返回 pending，不能当天卖。
    """
    cfg = config.normalized()
    if cost <= 0 or min(open_price, high, low, close) <= 0:
        return _none_plan()
    if pending is not None and pending.action == "pending":
        return _none_plan()

    evaluation = evaluate_day(
        high=high,
        low=low,
        open_=open_price,
        close=close,
        p_sell=cost,
        up_threshold=cfg.up_pct * 100.0,
        down_threshold=cfg.down_pct * 100.0,
    )
    if evaluation.barrier_hit is BarrierHit.EXPIRE:
        return _none_plan()

    if evaluation.barrier_hit is BarrierHit.DOWN:
        reason = _stop_reason(cfg)
        barrier: Literal["up", "down"] = "down"
        open_gap_done = open_price <= cost * (1.0 - cfg.down_pct)
    else:
        reason = _take_profit_reason(cfg)
        barrier = "up"
        open_gap_done = open_price >= cost * (1.0 + cfg.up_pct)

    if is_entry_day:
        return _pending_plan(
            reason=reason,
            trigger_price=evaluation.trigger_price,
            barrier=barrier,
        )

    # 开盘跳空已由 plan_open_exit 处理；若当时因跌停/停牌未成交，
    # 不在盘中用另一个价格重复尝试。
    if open_gap_done:
        return _none_plan()

    return _sell_plan(
        reason=reason,
        trigger_price=evaluation.trigger_price,
        fill_price=evaluation.trigger_price * (1.0 - cfg.slippage),
        barrier=barrier,
    )


def plan_tick_exit(
    *,
    last_price: float,
    cost: float,
    config: TripleBarrierConfig,
    held_days: int = 0,
    pending: ExitPlan | None = None,
    is_entry_day: bool = False,
) -> ExitPlan:
    """实盘 tick/最新价退出决策；成交由执行层按市价完成。"""
    cfg = config.normalized()
    if cost <= 0 or last_price <= 0:
        return _none_plan()

    if pending is not None and pending.action == "pending":
        return _sell_plan(
            reason=f"{pending.reason}(T+1补卖)",
            trigger_price=pending.trigger_price,
            fill_price=last_price,
            barrier=pending.barrier,
        )

    if held_days >= cfg.horizon:
        return _sell_plan(
            reason=f"触发三重障碍时间到期(持有{held_days}日)",
            trigger_price=last_price,
            fill_price=last_price,
            barrier="time",
        )

    down_price = cost * (1.0 - cfg.down_pct)
    up_price = cost * (1.0 + cfg.up_pct)
    if last_price <= down_price:
        if is_entry_day:
            return _pending_plan(
                reason=_stop_reason(cfg),
                trigger_price=down_price,
                barrier="down",
            )
        return _sell_plan(
            reason=_stop_reason(cfg),
            trigger_price=down_price,
            fill_price=last_price,
            barrier="down",
        )
    if last_price >= up_price:
        if is_entry_day:
            return _pending_plan(
                reason=_take_profit_reason(cfg),
                trigger_price=up_price,
                barrier="up",
            )
        return _sell_plan(
            reason=_take_profit_reason(cfg),
            trigger_price=up_price,
            fill_price=last_price,
            barrier="up",
        )
    return _none_plan()


def evaluate_window(
    bars: list[tuple[float, float, float, float]],
    p_sell: float,
    up_threshold: float,
    down_threshold: float,
) -> BarEvaluation:
    """评估 N 日窗口 (N = len(bars), N=0 时立即 expire 用当日 close).

    Args:
        bars: [(high, low, open, close), ...] 按时间顺序
        p_sell: 卖出价
        up_threshold: 上阈值 (百分点)
        down_threshold: 下阈值 (百分点)

    Returns:
        BarEvaluation: 首个触发日的评估, 或全窗口未触发时最后一日 close 的 expire
    """
    if not bars:
        raise ValueError("bars must not be empty")
    for high, low, open_, close in bars:
        ev = evaluate_day(high, low, open_, close, p_sell, up_threshold, down_threshold)
        if ev.barrier_hit != BarrierHit.EXPIRE:
            return ev
    last_close = bars[-1][3]
    return BarEvaluation(
        barrier_hit=BarrierHit.EXPIRE,
        excess_return=excess_return_expire(p_sell, last_close),
        trigger_price=last_close,
    )
