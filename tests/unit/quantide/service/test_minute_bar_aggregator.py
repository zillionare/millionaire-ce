"""盘中 30 分钟线内存合成测试。"""

from __future__ import annotations

import datetime

import pytest

from quantide.service.minute_bar_aggregator import MinuteBarAggregator


def _quote(price: float, volume: float, amount: float) -> dict:
    return {"price": price, "volume": volume, "amount": amount}


def _agg(calendar) -> MinuteBarAggregator:
    return MinuteBarAggregator(
        market_data=None,
        calendar=calendar,
        clock=lambda: datetime.datetime(2024, 1, 3, 10, 0),
    )


def test_aggregator_builds_bar_with_end_timestamp(calendar):
    """合成的 30 分钟线以 bar 收盘时刻为索引。"""
    agg = _agg(calendar)
    agg.on_sample(
        "600519.SH", _quote(10.0, 100, 1000), datetime.datetime(2024, 1, 3, 9, 31)
    )
    agg.on_sample(
        "600519.SH", _quote(11.0, 500, 5000), datetime.datetime(2024, 1, 3, 9, 59)
    )
    forming = agg.forming("600519.SH", datetime.datetime(2024, 1, 3, 9, 59))
    assert forming is not None
    assert forming["date"] == datetime.datetime(2024, 1, 3, 10, 0)
    assert forming["open"] == pytest.approx(10.0)
    assert forming["high"] == pytest.approx(11.0)
    assert forming["low"] == pytest.approx(10.0)
    assert forming["close"] == pytest.approx(11.0)
    assert forming["volume"] == pytest.approx(500.0)
    assert forming["amount"] == pytest.approx(5000.0)

    agg.on_sample(
        "600519.SH", _quote(11.2, 700, 7000), datetime.datetime(2024, 1, 3, 10, 1)
    )
    completed = agg.completed("600519.SH", datetime.datetime(2024, 1, 3, 10, 1))
    assert [row["date"] for row in completed] == [datetime.datetime(2024, 1, 3, 10, 0)]
    assert completed[0]["close"] == pytest.approx(11.0)
    forming = agg.forming("600519.SH", datetime.datetime(2024, 1, 3, 10, 1))
    assert forming is not None
    assert forming["date"] == datetime.datetime(2024, 1, 3, 10, 30)
    assert forming["volume"] == pytest.approx(200.0)
    assert forming["amount"] == pytest.approx(2000.0)


def test_aggregator_finalizes_last_bar_after_close(calendar):
    """15:00 后读取会完成最后一根 15:00 bar。"""
    agg = _agg(calendar)
    agg.on_sample(
        "600519.SH",
        _quote(10.0, 100, 1000),
        datetime.datetime(2024, 1, 3, 14, 58),
    )
    assert agg.forming("600519.SH", datetime.datetime(2024, 1, 3, 14, 58)) is not None
    completed = agg.completed("600519.SH", datetime.datetime(2024, 1, 3, 15, 1))
    assert [row["date"] for row in completed] == [datetime.datetime(2024, 1, 3, 15, 0)]
    assert agg.forming("600519.SH", datetime.datetime(2024, 1, 3, 15, 1)) is None


def test_aggregator_skips_lunch_and_non_trade_time(calendar):
    """午休/非交易时段不合成。"""
    agg = _agg(calendar)
    agg.on_sample(
        "600519.SH", _quote(10.0, 100, 1000), datetime.datetime(2024, 1, 3, 12, 0)
    )
    assert agg.completed("600519.SH", datetime.datetime(2024, 1, 3, 12, 1)) == []
    assert agg.forming("600519.SH", datetime.datetime(2024, 1, 3, 12, 1)) is None


def test_aggregator_clear_between(calendar):
    """BaoStock 入库后丢弃对应日期的内存数据。"""
    agg = _agg(calendar)
    agg.on_sample(
        "600519.SH", _quote(10.0, 100, 1000), datetime.datetime(2024, 1, 3, 9, 31)
    )
    agg.on_sample(
        "600519.SH", _quote(10.5, 200, 2000), datetime.datetime(2024, 1, 3, 10, 1)
    )
    agg.clear_between(datetime.date(2024, 1, 3), datetime.date(2024, 1, 3))
    assert agg.completed("600519.SH", datetime.datetime(2024, 1, 3, 10, 30)) == []
    assert agg.forming("600519.SH", datetime.datetime(2024, 1, 3, 10, 30)) is None
