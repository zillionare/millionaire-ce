"""腾讯行情发布者的交易时段门控测试。"""

from __future__ import annotations

import datetime

import pytest

from quantide.providers.tencent_publisher import TencentPublisher


class _Calendar:
    def __init__(self, is_trade_day: bool = True) -> None:
        self._is_trade_day = is_trade_day

    def is_trade_day(self, now) -> bool:
        return self._is_trade_day


def _publisher(trade_day: bool = True) -> TencentPublisher:
    return TencentPublisher(
        ["600519.SH"],
        fetcher=object(),  # type: ignore[arg-type]
        calendar=_Calendar(trade_day),
    )


@pytest.mark.parametrize(
    "tm,expected",
    [
        (datetime.time(9, 14, 59), False),
        (datetime.time(9, 15), True),
        (datetime.time(10, 0), True),
        (datetime.time(11, 30), True),
        (datetime.time(11, 30, 1), False),
        (datetime.time(12, 0), False),
        (datetime.time(13, 0), True),
        (datetime.time(14, 57), True),
        (datetime.time(15, 0), True),
        (datetime.time(15, 0, 30), True),
        (datetime.time(15, 0, 31), False),
        (datetime.time(20, 0), False),
    ],
)
def test_in_session_window(tm, expected):
    publisher = _publisher()
    moment = datetime.datetime.combine(datetime.date(2024, 1, 3), tm)
    assert publisher._in_session(moment) is expected


def test_in_session_rejects_non_trade_day():
    publisher = _publisher(trade_day=False)
    moment = datetime.datetime(2024, 1, 6, 10, 0)
    assert publisher._in_session(moment) is False


def test_tick_skips_requests_when_closed(monkeypatch):
    publisher = _publisher()
    calls: list[int] = []
    monkeypatch.setattr(publisher, "publish_once", lambda: calls.append(1) or 1)

    assert publisher._tick(datetime.datetime(2024, 1, 3, 12, 0)) == 0
    assert calls == []

    assert publisher._tick(datetime.datetime(2024, 1, 3, 10, 0)) == 1
    assert calls == [1]
