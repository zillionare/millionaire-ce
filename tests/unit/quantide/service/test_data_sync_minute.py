"""30 分钟同步（BaoStock-only）与日期边界测试。"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from quantide.service.data_sync import (
    MINUTE_HISTORY_START,
    DataSyncService,
    SyncParams,
    _minute_history_end,
)


@pytest.fixture(autouse=True)
def _global_calendar(asset_dir):
    from quantide.data.models.calendar import calendar as global_calendar

    global_calendar.load(asset_dir / "baseline_calendar.parquet")
    return global_calendar


@pytest.mark.parametrize(
    "end_date,now,expected",
    [
        (datetime.date(2024, 1, 3), datetime.datetime(2024, 1, 3, 14, 0), datetime.date(2024, 1, 2)),
        (datetime.date(2024, 1, 3), datetime.datetime(2024, 1, 3, 18, 0), datetime.date(2024, 1, 3)),
        (datetime.date(2024, 1, 6), datetime.datetime(2024, 1, 6, 10, 0), datetime.date(2024, 1, 4)),
    ],
)
def test_minute_history_end(end_date, now, expected):
    """当日 17:30 前不用当日数据；非交易日回退。"""
    assert _minute_history_end(end_date, now) == expected


def test_minute_history_end_past_date():
    """历史日期直接使用。"""
    now = datetime.datetime(2026, 1, 1, 10, 0)
    assert _minute_history_end(datetime.date(2024, 1, 2), now) == datetime.date(
        2024, 1, 2
    )


class _FakeStore:
    def __init__(self, last_dates):
        self._last_dates = last_dates
        self.appended: list[pd.DataFrame] = []

    @property
    def end(self):
        return max(self._last_dates.values()) if self._last_dates else None

    def last_dates(self):
        return dict(self._last_dates)

    def append_data(self, frame):
        self.appended.append(frame)


class _FakeFetcher:
    def close(self):
        pass


class _FakeAdapter:
    calls: list[tuple[str, datetime.date, datetime.date]] = []

    def __init__(self, fetcher):
        pass

    def fetch_minute_bars(self, asset, start_date, end_date, period="30", count=320):
        type(self).calls.append((asset, start_date, end_date))
        return (
            pd.DataFrame(
                {
                    "date": [pd.Timestamp("2024-01-03 10:00:00")],
                    "asset": [asset],
                    "open": [10.0],
                    "high": [10.0],
                    "low": [10.0],
                    "close": [10.0],
                    "volume": [100.0],
                    "amount": [1000.0],
                }
            ),
            [],
        )


class _FakeAggregator:
    cleared: list[tuple[datetime.date, datetime.date]] = []

    def clear_between(self, start, end):
        type(self).cleared.append((start, end))


def _patch(monkeypatch, store):
    monkeypatch.setattr("quantide.data.get_minute_bars_store", lambda: store)
    monkeypatch.setattr(
        "quantide.data.fetchers.baostock_minute.BaostockMinuteFetcher",
        _FakeFetcher,
    )
    monkeypatch.setattr(
        "quantide.data.fetchers.multi.BaostockMinuteAdapter", _FakeAdapter
    )
    monkeypatch.setattr(
        "quantide.service.minute_bar_aggregator.minute_bar_aggregator",
        _FakeAggregator(),
    )
    _FakeAdapter.calls = []
    _FakeAggregator.cleared = []


def test_sync_backfills_per_asset_from_last_date(monkeypatch):
    """每只标的从自身最后日期+1 开始补，避免新标的缺历史。"""
    store = _FakeStore({"600519.SH": datetime.date(2024, 1, 2)})
    _patch(monkeypatch, store)
    params = SyncParams(
        start_date=datetime.date(2015, 1, 1),
        end_date=datetime.date(2024, 1, 4),
        minute_assets=["600519.SH"],
    )
    DataSyncService()._sync_minute_bars(params)
    assert _FakeAdapter.calls == [
        ("600519.SH", datetime.date(2024, 1, 3), datetime.date(2024, 1, 4))
    ]
    assert len(store.appended) == 1
    assert _FakeAggregator.cleared == [
        (MINUTE_HISTORY_START, datetime.date(2024, 1, 4))
    ]


def test_sync_skips_covered_asset(monkeypatch):
    """已覆盖到结束日的标的不再请求。"""
    store = _FakeStore({"600519.SH": datetime.date(2024, 1, 4)})
    _patch(monkeypatch, store)
    params = SyncParams(
        start_date=datetime.date(2015, 1, 1),
        end_date=datetime.date(2024, 1, 4),
        minute_assets=["600519.SH"],
    )
    DataSyncService()._sync_minute_bars(params)
    assert _FakeAdapter.calls == []
    assert store.appended == []
