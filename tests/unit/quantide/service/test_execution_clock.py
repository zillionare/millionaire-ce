"""交易时钟（集合竞价窗口）的单元测试。"""

from __future__ import annotations

import datetime

from quantide.service.execution_clock import (
    AUCTION_END,
    AUCTION_ORDER_DEADLINE,
    AUCTION_START,
    ExecutionClock,
    execution_clock,
)


class _StubCalendar:
    """只含交易日的迷你日历。"""

    def __init__(self, days: list[datetime.date]) -> None:
        self._days = days

    @property
    def day_frames(self):
        """交易日序列。"""
        import pyarrow as pa

        return pa.array(self._days, type=pa.date32())


def _clock(days: list[datetime.date] | None = None) -> ExecutionClock:
    """构造时钟。"""
    if days is None:
        days = [datetime.date(2024, 1, 2), datetime.date(2024, 1, 3)]
    return ExecutionClock(calendar=_StubCalendar(days))


def test_auction_window_detected():
    """14:57-15:00 判定为集合竞价。"""
    clock = _clock()
    at = datetime.datetime(2024, 1, 2, 14, 58, 30)
    phase = clock.phase(at)
    assert phase.is_auction is True
    assert phase.order_allowed is True


def test_before_auction_is_continuous():
    """14:57 之前是连续竞价。"""
    clock = _clock()
    phase = clock.phase(datetime.datetime(2024, 1, 2, 10, 0))
    assert phase.is_auction is False
    assert phase.order_allowed is True


def test_after_auction_window_is_post_close():
    """15:00 之后收盘，不再下单。"""
    clock = _clock()
    phase = clock.phase(datetime.datetime(2024, 1, 2, 15, 30))
    assert phase.name == "post_close"
    assert phase.order_allowed is False


def test_pre_open_does_not_trade():
    """开盘前不允许下单。"""
    clock = _clock()
    phase = clock.phase(datetime.datetime(2024, 1, 2, 8, 0))
    assert phase.name == "pre_open"
    assert phase.order_allowed is False


def test_non_trading_day_is_closed():
    """非交易日全程关闭。"""
    clock = _clock(days=[datetime.date(2024, 1, 2)])
    phase = clock.phase(datetime.datetime(2024, 1, 3, 14, 58))
    assert phase.name == "closed"
    assert phase.order_allowed is False


def test_auction_price_limit_applies_epsilon():
    """集合竞价限价 = 竞价价 × (1+eps)。"""
    clock = _clock()
    assert clock.auction_price_limit(100.0, 0.001) == 100.1
    assert clock.auction_price_limit(100.0, 0.0) == 100.0
    assert clock.auction_price_limit(50.0, 0.002) == 50.1


def test_seconds_to_auction_end():
    """距离集合竞价结束的秒数。"""
    clock = _clock()
    at = datetime.datetime(2024, 1, 2, 14, 58, 0)
    assert 100 <= clock.seconds_to_auction_end(at) <= 121


def test_trading_day_cache_is_reused():
    """交易日集合只构建一次。"""
    clock = _clock()
    clock.is_trading_day(datetime.date(2024, 1, 2))
    cached = clock._trading_days
    clock.is_trading_day(datetime.date(2024, 1, 3))
    assert clock._trading_days is cached


def test_constants_match_a_share_session():
    """集合竞价时间常量符合 A 股规则（14:57-15:00）。"""
    assert AUCTION_START == datetime.time(14, 57)
    assert AUCTION_END == datetime.time(15, 0)
    assert AUCTION_ORDER_DEADLINE == datetime.time(14, 59)


def test_global_clock_available():
    """全局单例可用。"""
    assert isinstance(execution_clock, ExecutionClock)
