"""分钟线在 BarView / Broker 中的读取链路测试（离线）。"""

from __future__ import annotations

import datetime

import polars as pl
import pytest

from quantide.core.enums import FrameType
from quantide.service.bar_views import HistBarView, LiveBarView


@pytest.fixture(autouse=True)
def _global_calendar(calendar):
    """测试内加载全局日历（视图/合成器默认使用单例）。"""
    from quantide.data.models.calendar import calendar as global_calendar

    global_calendar.load(calendar._path)
    return global_calendar


def _completed_minute() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [datetime.datetime(2024, 1, 3, 10, 0)],
            "asset": ["600519.SH"],
            "open": [10.0],
            "high": [10.6],
            "low": [9.9],
            "close": [10.5],
            "volume": [100.0],
            "amount": [1000.0],
            "adjust": [1.0],
            "is_st": [False],
            "up_limit": [11.55],
            "down_limit": [9.45],
        }
    ).with_columns(pl.col("date").cast(pl.Datetime("ms")))


class _FakeMinuteModel:
    def get_bars(self, n, end=None, assets=None, adjust="qfq"):
        frame = _completed_minute()
        if isinstance(end, datetime.datetime):
            frame = frame.filter(pl.col("date") <= end)
        return frame


class _FakeMarketData:
    def snapshot(self, symbols):
        return {
            "600519.SH": {
                "price": 11.0,
                "open": 10.0,
                "high": 11.2,
                "low": 9.8,
                "volume": 500.0,
                "amount": 5000.0,
                "ts": datetime.datetime(2024, 1, 3, 10, 5),
            }
        }

    def get_limits(self, asset):
        return 12.0, 9.0


class _FakeAggregator:
    def __init__(self, completed=None, forming=None):
        self._completed = list(completed or [])
        self._forming = forming
        self.subscribed: list[str] = []

    def subscribe(self, assets):
        self.subscribed.extend(assets)

    def completed(self, asset, end=None, count=None):
        return [
            row
            for row in self._completed
            if end is None or row["date"] <= end
        ]

    def forming(self, asset, now=None):
        if self._forming is None:
            return None
        if now is not None and self._forming["date"] <= now:
            return None
        return self._forming


def _patch_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "quantide.data.models.minute_bars.minute_bars", _FakeMinuteModel()
    )


def test_hist_bar_view_reads_minute_history(monkeypatch):
    """回测视图按 30m 读取完成分钟线（bar 收盘时刻）。"""
    _patch_model(monkeypatch)
    view = HistBarView()
    frame = view.get_history(
        "600519.SH", 2, datetime.datetime(2024, 1, 3, 10, 5), "30m"
    )
    assert len(frame) == 1
    assert frame["date"][0] == datetime.datetime(2024, 1, 3, 10, 0)
    assert frame["close"][0] == 10.5


def test_live_bar_view_injects_forming_minute_bucket(monkeypatch):
    """实盘视图把当前未完成桶（10:00-10:30）合进分钟线。"""
    _patch_model(monkeypatch)
    aggregator = _FakeAggregator(
        forming={
            "date": datetime.datetime(2024, 1, 3, 10, 30),
            "asset": "600519.SH",
            "open": 10.5,
            "high": 11.2,
            "low": 10.4,
            "close": 11.0,
            "volume": 400.0,
            "amount": 4000.0,
        }
    )
    view = LiveBarView(
        market_data=_FakeMarketData(),
        today_func=lambda: datetime.date(2024, 1, 3),
        minute_aggregator=aggregator,
    )
    frame = view.get_history(
        "600519.SH",
        2,
        datetime.datetime(2024, 1, 3, 10, 5),
        "30m",
        include_forming_bar=True,
    )
    frame = frame.sort("date")
    assert len(frame) == 2
    forming = frame.row(-1, named=True)
    assert forming["date"] == datetime.datetime(2024, 1, 3, 10, 30)
    assert forming["open"] == pytest.approx(10.5)
    assert forming["close"] == pytest.approx(11.0)
    assert forming["volume"] == pytest.approx(400.0)
    assert forming["amount"] == pytest.approx(4000.0)
    assert forming["up_limit"] == pytest.approx(12.0)
    assert set(aggregator.subscribed) == {"600519.SH"}


def test_live_bar_view_includes_completed_memory_bars(monkeypatch):
    """当日已完成桶来自内存合成器（历史优先）。"""
    _patch_model(monkeypatch)
    aggregator = _FakeAggregator(
        completed=[
            {
                "date": datetime.datetime(2024, 1, 3, 10, 30),
                "asset": "600519.SH",
                "open": 10.5,
                "high": 10.8,
                "low": 10.4,
                "close": 10.7,
                "volume": 200.0,
                "amount": 2000.0,
            }
        ]
    )
    view = LiveBarView(
        market_data=_FakeMarketData(),
        today_func=lambda: datetime.date(2024, 1, 3),
        minute_aggregator=aggregator,
    )
    frame = view.get_history(
        "600519.SH",
        3,
        datetime.datetime(2024, 1, 3, 10, 31),
        FrameType.MIN30,
        include_forming_bar=False,
    )
    frame = frame.sort("date")
    assert len(frame) == 2
    assert frame["date"][-1] == datetime.datetime(2024, 1, 3, 10, 30)
    assert frame["close"][-1] == pytest.approx(10.7)


def test_live_bar_view_minute_without_forming(monkeypatch):
    """include_forming_bar=False 时不注入当前桶。"""
    _patch_model(monkeypatch)
    aggregator = _FakeAggregator(
        forming={
            "date": datetime.datetime(2024, 1, 3, 10, 30),
            "asset": "600519.SH",
            "open": 10.5,
            "high": 11.2,
            "low": 10.4,
            "close": 11.0,
            "volume": 400.0,
            "amount": 4000.0,
        }
    )
    view = LiveBarView(
        market_data=_FakeMarketData(),
        today_func=lambda: datetime.date(2024, 1, 3),
        minute_aggregator=aggregator,
    )
    frame = view.get_history(
        "600519.SH",
        2,
        datetime.datetime(2024, 1, 3, 10, 5),
        "30m",
        include_forming_bar=False,
    )
    assert len(frame) == 1


@pytest.mark.parametrize(
    "tm,expected",
    [
        (datetime.datetime(2024, 1, 3, 9, 31), datetime.datetime(2024, 1, 3, 10, 0)),
        (datetime.datetime(2024, 1, 3, 10, 5), datetime.datetime(2024, 1, 3, 10, 30)),
        (datetime.datetime(2024, 1, 3, 11, 30), datetime.datetime(2024, 1, 3, 11, 30)),
        (datetime.datetime(2024, 1, 3, 13, 5), datetime.datetime(2024, 1, 3, 13, 30)),
        (datetime.datetime(2024, 1, 3, 14, 57), datetime.datetime(2024, 1, 3, 15, 0)),
    ],
)
def test_bucket_end(tm, expected):
    """分钟桶按 Calendar 收盘时刻取整（含午休跳过）。"""
    assert LiveBarView._bucket_end(tm, FrameType.MIN30) == expected


def test_paper_broker_delegates_minute_history(monkeypatch):
    """PaperBroker.get_history 支持 30m 周期并委托给 LiveBarView。"""
    from quantide.data.sqlite import db
    from quantide.service.sim_broker import PaperBroker

    db.init(":memory:")
    broker = PaperBroker(portfolio_id="minute-unit", principal=100_000)
    try:
        monkeypatch.setattr(
            broker._bar_view,
            "get_minute_history",
            lambda *args, **kwargs: _completed_minute(),
        )
        frame = broker.get_history(
            "600519.SH",
            2,
            datetime.datetime(2024, 1, 3, 10, 5),
            "30m",
        )
        assert len(frame) == 1
        assert frame["close"][0] == 10.5
    finally:
        broker.close()


class _FakeDailyClose:
    """上一交易日原始日线（用于除权日识别）。"""

    def __init__(self, close: float) -> None:
        self._close = close

    def get_bars(self, n, end=None, assets=None, adjust=None):
        return pl.DataFrame(
            {
                "date": [datetime.date(2024, 1, 2)],
                "asset": ["600519.SH"],
                "open": [self._close],
                "high": [self._close],
                "low": [self._close],
                "close": [self._close],
                "volume": [1000.0],
                "amount": [100000.0],
                "adjust": [2.0],
                "is_st": [False],
                "up_limit": [110.0],
                "down_limit": [90.0],
            }
        ).with_columns(pl.col("date").cast(pl.Date))


class _ExRightMarketData:
    def __init__(self, prev_close: float | None) -> None:
        self._prev_close = prev_close

    def snapshot(self, symbols):
        return {
            "600519.SH": {
                "price": 95.5,
                "open": 95.2,
                "high": 96.0,
                "low": 95.0,
                "volume": 200.0,
                "amount": 2000.0,
                "prev_close": self._prev_close,
                "ts": datetime.datetime(2024, 1, 3, 10, 5),
            }
        }

    def get_limits(self, asset):
        return 100.0, 90.0


class _StaticMinuteModel:
    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame

    def get_bars(self, n, end=None, assets=None, adjust="qfq"):
        frame = self._frame
        if isinstance(end, datetime.datetime):
            frame = frame.filter(pl.col("date") <= end)
        return frame


def _history_jan2() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [datetime.datetime(2024, 1, 2, 15, 0)],
            "asset": ["600519.SH"],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [100.0],
            "amount": [10000.0],
            "adjust": [2.0],
            "is_st": [False],
            "up_limit": [110.0],
            "down_limit": [90.0],
        }
    ).with_columns(pl.col("date").cast(pl.Datetime("ms")))


def _forming_jan3() -> _FakeAggregator:
    return _FakeAggregator(
        forming={
            "date": datetime.datetime(2024, 1, 3, 10, 30),
            "asset": "600519.SH",
            "open": 95.2,
            "high": 96.0,
            "low": 95.0,
            "close": 95.5,
            "volume": 20.0,
            "amount": 2000.0,
        }
    )


def test_live_minute_rebases_history_on_ex_right_day(monkeypatch):
    """除权日把 qfq 历史换算到今日基准，今日 forming 保持原始价。"""
    monkeypatch.setattr(
        "quantide.data.models.minute_bars.minute_bars",
        _StaticMinuteModel(_history_jan2()),
    )
    view = LiveBarView(
        bars=_FakeDailyClose(100.0),
        market_data=_ExRightMarketData(95.0),
        today_func=lambda: datetime.date(2024, 1, 3),
        minute_aggregator=_forming_jan3(),
    )
    frame = view.get_history(
        "600519.SH",
        5,
        datetime.datetime(2024, 1, 3, 10, 5),
        FrameType.MIN30,
        include_forming_bar=True,
    ).sort("date")
    history = frame.filter(pl.col("date").dt.date() == datetime.date(2024, 1, 2)).row(0, named=True)
    forming = frame.filter(pl.col("date").dt.date() == datetime.date(2024, 1, 3)).row(0, named=True)
    assert history["close"] == pytest.approx(95.0)
    assert history["open"] == pytest.approx(95.0)
    assert history["up_limit"] == pytest.approx(104.5)
    assert history["volume"] == pytest.approx(100.0 / 0.95)
    assert forming["close"] == pytest.approx(95.5)
    assert forming["volume"] == pytest.approx(20.0)


def test_live_minute_no_rebase_on_normal_day(monkeypatch):
    """非除权日（昨收=昨日收盘）不做任何缩放。"""
    monkeypatch.setattr(
        "quantide.data.models.minute_bars.minute_bars",
        _StaticMinuteModel(_history_jan2()),
    )
    view = LiveBarView(
        bars=_FakeDailyClose(100.0),
        market_data=_ExRightMarketData(100.0),
        today_func=lambda: datetime.date(2024, 1, 3),
        minute_aggregator=_forming_jan3(),
    )
    frame = view.get_history(
        "600519.SH",
        5,
        datetime.datetime(2024, 1, 3, 10, 5),
        FrameType.MIN30,
    )
    history = frame.filter(pl.col("date").dt.date() == datetime.date(2024, 1, 2)).row(0, named=True)
    assert history["close"] == pytest.approx(100.0)
    assert history["volume"] == pytest.approx(100.0)


def test_live_minute_without_prev_close_keeps_old_behavior(monkeypatch):
    """发布者未提供 prev_close 时退回原逻辑。"""
    monkeypatch.setattr(
        "quantide.data.models.minute_bars.minute_bars",
        _StaticMinuteModel(_history_jan2()),
    )
    view = LiveBarView(
        bars=_FakeDailyClose(100.0),
        market_data=_ExRightMarketData(None),
        today_func=lambda: datetime.date(2024, 1, 3),
        minute_aggregator=_forming_jan3(),
    )
    frame = view.get_history(
        "600519.SH",
        5,
        datetime.datetime(2024, 1, 3, 10, 5),
        FrameType.MIN30,
    )
    history = frame.filter(pl.col("date").dt.date() == datetime.date(2024, 1, 2)).row(0, named=True)
    assert history["close"] == pytest.approx(100.0)


def test_live_minute_skips_rebase_when_today_already_stored(monkeypatch):
    """盘后同步已含今日数据时，qfq 基准已是今日因子，不能重复缩放。"""
    frame_with_today = pl.concat(
        [
            _history_jan2(),
            pl.DataFrame(
                {
                    "date": [datetime.datetime(2024, 1, 3, 10, 0)],
                    "asset": ["600519.SH"],
                    "open": [95.5],
                    "high": [96.0],
                    "low": [95.0],
                    "close": [95.5],
                    "volume": [200.0],
                    "amount": [2000.0],
                    "adjust": [1.9],
                    "is_st": [False],
                    "up_limit": [104.5],
                    "down_limit": [85.5],
                }
            ).with_columns(pl.col("date").cast(pl.Datetime("ms"))),
        ],
        how="vertical_relaxed",
    )
    monkeypatch.setattr(
        "quantide.data.models.minute_bars.minute_bars",
        _StaticMinuteModel(frame_with_today),
    )
    view = LiveBarView(
        bars=_FakeDailyClose(100.0),
        market_data=_ExRightMarketData(95.0),
        today_func=lambda: datetime.date(2024, 1, 3),
        minute_aggregator=_forming_jan3(),
    )
    frame = view.get_history(
        "600519.SH",
        5,
        datetime.datetime(2024, 1, 3, 14, 0),
        FrameType.MIN30,
        include_forming_bar=False,
    ).sort("date")
    history = frame.filter(pl.col("date").dt.date() == datetime.date(2024, 1, 2)).row(0, named=True)
    today = frame.filter(pl.col("date").dt.date() == datetime.date(2024, 1, 3)).row(0, named=True)
    assert history["close"] == pytest.approx(100.0)
    assert today["close"] == pytest.approx(95.5)
