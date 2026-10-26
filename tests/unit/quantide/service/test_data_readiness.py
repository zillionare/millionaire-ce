"""数据就绪检查测试。"""

from __future__ import annotations

import datetime

import polars as pl

from quantide.service.data_readiness import DataReadinessMonitor


class _Calendar:
    def __init__(self, prev: datetime.date) -> None:
        self._prev = prev

    def is_trade_day(self, now) -> bool:
        return True

    def day_shift(self, day, n, frame_type=None):
        return self._prev


class _Daily:
    def __init__(self, end, frame=None) -> None:
        self.end = end
        self.frame = frame

    def get_bars_in_range(self, start, end=None, assets=None, adjust=None, eager_mode=True):
        if self.frame is None:
            return pl.DataFrame()
        return self.frame


class _Limits:
    def __init__(self, count: int) -> None:
        self.count = count

    def limits_map(self, day):
        return {f"{i:06d}.SZ": (9.0, 11.0) for i in range(self.count)}


class _Stocks:
    size = 2


class _Quotes:
    def __init__(self, last) -> None:
        self.last = last

    def status(self):
        return {"started": True, "last_received_at": self.last}


def _aux_frame(rows: int = 2) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [datetime.date(2024, 1, 2)] * rows,
            "asset": [f"{i:06d}.SZ" for i in range(rows)],
            "adjust": [2.0] * rows,
            "up_limit": [11.0] * rows,
            "down_limit": [9.0] * rows,
        }
    )


def _monitor(calendar, daily, limits, quotes=None, events=None):
    events = events if events is not None else []
    monitor = DataReadinessMonitor(
        daily_store=daily,
        limit_store_factory=lambda: limits,
        stocks=_Stocks(),
        calendar=calendar,
        quote_monitor=quotes or _Quotes(datetime.datetime(2024, 1, 3, 9, 24)),
        clock=lambda: datetime.datetime(2024, 1, 3, 9, 25),
        recorder=lambda **kwargs: events.append(kwargs),
    )
    return monitor, events


def test_morning_check_ok_records_nothing():
    monitor, events = _monitor(
        _Calendar(datetime.date(2024, 1, 2)),
        _Daily(datetime.date(2024, 1, 2), _aux_frame()),
        _Limits(2),
    )
    monitor.check_morning(datetime.datetime(2024, 1, 3, 9, 25))
    assert events == []


def test_morning_check_records_failures_once_and_recovers():
    calendar = _Calendar(datetime.date(2024, 1, 2))
    daily = _Daily(datetime.date(2024, 1, 1), None)
    limits = _Limits(0)
    monitor, events = _monitor(calendar, daily, limits)

    monitor.check_morning(datetime.datetime(2024, 1, 3, 9, 25))
    assert len(events) == 3
    assert all(event["blocked"] for event in events)
    assert {event["target_id"] for event in events} == {
        "data:daily_freshness",
        "data:daily_aux",
        "data:limit_today",
    }

    monitor.check_morning(datetime.datetime(2024, 1, 3, 9, 55))
    assert len(events) == 3  # 不重复告警

    daily.end = datetime.date(2024, 1, 2)
    daily.frame = _aux_frame()
    limits.count = 2
    monitor.check_morning(datetime.datetime(2024, 1, 3, 10, 25))
    assert len(events) == 6
    assert all(not event["blocked"] for event in events[3:])


def test_quote_check_stale_and_recovered():
    now = datetime.datetime(2024, 1, 3, 10, 0)
    quotes = _Quotes(now - datetime.timedelta(seconds=120))
    monitor, events = _monitor(
        _Calendar(datetime.date(2024, 1, 2)),
        _Daily(datetime.date(2024, 1, 2), _aux_frame()),
        _Limits(2),
        quotes=quotes,
    )
    monitor.check_quotes(now)
    assert len(events) == 1
    assert events[0]["title"] == "实时行情中断"

    monitor.check_quotes(now)
    assert len(events) == 1

    quotes.last = now
    monitor.check_quotes(now + datetime.timedelta(minutes=1))
    assert len(events) == 2
    assert events[1]["blocked"] is False


def test_quote_check_skipped_outside_session():
    monitor, events = _monitor(
        _Calendar(datetime.date(2024, 1, 2)),
        _Daily(datetime.date(2024, 1, 2), _aux_frame()),
        _Limits(2),
        quotes=_Quotes(None),
    )
    monitor.check_quotes(datetime.datetime(2024, 1, 3, 12, 0))
    assert events == []


def test_eod_check_skipped_before_1830():
    monitor, events = _monitor(
        _Calendar(datetime.date(2024, 1, 2)),
        _Daily(datetime.date(2024, 1, 2), None),
        _Limits(0),
    )
    monitor.check_end_of_day(datetime.datetime(2024, 1, 3, 18, 0))
    assert events == []


def test_system_event_resolution_clears_header_alarm():
    """恢复事件会把同一 target 的未解决告警标记为已解决。"""
    from quantide.service.strategy_runtime import StrategyRuntimeManager

    manager = StrategyRuntimeManager()
    manager._save_specs = lambda: None  # 不写真实状态文件
    manager.record_system_event(
        title="当日涨跌停缺失",
        message="missing",
        severity="critical",
        blocked=True,
        target_id="data:limit_today",
    )
    assert manager.risk_summary()["open_events"] == 1
    manager.record_system_event(
        title="数据恢复：当日涨跌停缺失",
        message="ok",
        severity="info",
        blocked=False,
        target_id="data:limit_today",
    )
    assert manager.risk_summary()["open_events"] == 0


def test_daily_aux_detects_all_one_adjust_factor():
    """复权因子全被填充为 1.0 时应告警。"""
    frame = _aux_frame().with_columns(pl.lit(1.0).alias("adjust"))
    monitor, events = _monitor(
        _Calendar(datetime.date(2024, 1, 2)),
        _Daily(datetime.date(2024, 1, 2), frame),
        _Limits(2),
    )
    monitor.check_morning(datetime.datetime(2024, 1, 3, 9, 25))
    assert any(event["target_id"] == "data:daily_aux" for event in events)
