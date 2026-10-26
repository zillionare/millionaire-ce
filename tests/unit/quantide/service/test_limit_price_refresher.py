"""当日涨跌停刷新（Tushare 早晨）测试。"""

from __future__ import annotations

import datetime

import pandas as pd
import polars as pl

from quantide.core.enums import Topics
from quantide.service.limit_price_refresher import LimitPriceRefresher


class _FakeFetcher:
    def __init__(self, frame):
        self._frame = frame
        self.calls: list[datetime.date] = []

    def fetch_limit_price(self, dt):
        self.calls.append(dt)
        return self._frame, []


class _FakeStore:
    def __init__(self):
        self.frames: list[pl.DataFrame] = []

    def append_data(self, frame):
        self.frames.append(frame)


def _refresher(calendar, fetcher, store):
    return LimitPriceRefresher(
        fetcher_factory=lambda: fetcher,
        store_factory=lambda: store,
        calendar=calendar,
        clock=lambda: datetime.datetime(2024, 1, 3, 9, 0),
    )


def test_refresh_writes_store_and_publishes(calendar, monkeypatch):
    """当日限价写入存储并广播双键名。"""
    frame = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-03")],
            "asset": ["600519.SH"],
            "up_limit": [11.0],
            "down_limit": [9.0],
        }
    )
    fetcher = _FakeFetcher(frame)
    store = _FakeStore()
    published: list = []
    refresher = _refresher(calendar, fetcher, store)
    monkeypatch.setattr("quantide.service.limit_price_refresher.msg_hub.publish", lambda topic, payload: published.append((topic, payload)))

    assert refresher.refresh(datetime.date(2024, 1, 3)) is True
    assert fetcher.calls == [datetime.date(2024, 1, 3)]
    assert len(store.frames) == 1
    assert store.frames[0]["up_limit"][0] == 11.0
    assert published[0][0] == Topics.STOCK_LIMIT.value
    assert published[0][1]["600519.SH"]["up"] == 11.0
    assert published[0][1]["600519.SH"]["up_limit"] == 11.0
    assert published[0][1]["600519.SH"]["down"] == 9.0


def test_refresh_empty_returns_false(calendar, monkeypatch):
    """空结果不写存储、标记未成功。"""
    fetcher = _FakeFetcher(pd.DataFrame())
    store = _FakeStore()
    refresher = _refresher(calendar, fetcher, store)
    monkeypatch.setattr("quantide.service.limit_price_refresher.msg_hub.publish", lambda *a, **k: None)

    assert refresher.refresh(datetime.date(2024, 1, 3)) is False
    assert store.frames == []
    assert refresher._last_success is None


def test_refresh_skips_non_trade_day(calendar, monkeypatch):
    """非交易日直接跳过。"""
    fetcher = _FakeFetcher(pd.DataFrame())
    store = _FakeStore()
    refresher = _refresher(calendar, fetcher, store)
    monkeypatch.setattr("quantide.service.limit_price_refresher.msg_hub.publish", lambda *a, **k: None)

    assert refresher.refresh(datetime.date(2024, 1, 6)) is False
    assert fetcher.calls == []


def test_0920_retry_only_when_needed(calendar, monkeypatch):
    """09:20 仅在 09:00 未成功时补拉。"""
    frame = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-03")],
            "asset": ["600519.SH"],
            "up_limit": [11.0],
            "down_limit": [9.0],
        }
    )
    fetcher = _FakeFetcher(frame)
    store = _FakeStore()
    refresher = _refresher(calendar, fetcher, store)
    monkeypatch.setattr("quantide.service.limit_price_refresher.msg_hub.publish", lambda *a, **k: None)

    refresher._last_success = datetime.date(2024, 1, 3)
    refresher._refresh_if_needed()
    assert fetcher.calls == []

    refresher._last_success = datetime.date(2024, 1, 2)
    refresher._refresh_if_needed()
    assert fetcher.calls == [datetime.date(2024, 1, 3)]


def test_start_registers_misfire_tolerant_jobs(calendar, monkeypatch):
    """定时任务允许休眠后补跑（misfire_grace_time=None）。"""
    import quantide.service.limit_price_refresher as module

    calls: list[dict] = []
    monkeypatch.setattr(
        module.scheduler, "add_job", lambda *a, **k: calls.append(k)
    )
    fetcher = _FakeFetcher(pd.DataFrame())
    store = _FakeStore()
    refresher = _refresher(calendar, fetcher, store)
    monkeypatch.setattr(
        "quantide.service.limit_price_refresher.msg_hub.publish", lambda *a, **k: None
    )
    refresher.start()
    assert len(calls) == 2
    assert all(call.get("misfire_grace_time") is None for call in calls)
    assert all(call.get("coalesce") is True for call in calls)
