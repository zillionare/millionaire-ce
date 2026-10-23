"""分钟线在 BarView / Broker 中的读取链路测试（离线）。"""

from __future__ import annotations

import datetime

import polars as pl
import pytest

from quantide.service.bar_views import HistBarView, LiveBarView


def _completed_minute() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [datetime.datetime(2024, 1, 3, 9, 30)],
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
        return _completed_minute()


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


def _patch_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "quantide.data.models.minute_bars.minute_bars", _FakeMinuteModel()
    )


def test_hist_bar_view_reads_minute_history(monkeypatch):
    """回测视图按 30m 读取完成分钟线。"""
    _patch_model(monkeypatch)
    view = HistBarView()
    frame = view.get_history(
        "600519.SH", 2, datetime.datetime(2024, 1, 3, 10, 5), "30m"
    )
    assert len(frame) == 1
    assert frame["close"][0] == 10.5


def test_live_bar_view_injects_forming_minute_bucket(monkeypatch):
    """实盘视图把当前 30 分钟桶（10:00-10:30）合进分钟线。"""
    _patch_model(monkeypatch)
    view = LiveBarView(
        market_data=_FakeMarketData(),
        today_func=lambda: datetime.date(2024, 1, 3),
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
    assert forming["date"] == datetime.datetime(2024, 1, 3, 10, 0)
    assert forming["open"] == pytest.approx(10.5)
    assert forming["close"] == pytest.approx(11.0)
    assert forming["volume"] == pytest.approx(400.0)  # 500 - 100
    assert forming["amount"] == pytest.approx(4000.0)  # 5000 - 1000


def test_live_bar_view_minute_without_forming(monkeypatch):
    """include_forming_bar=False 时不注入当前桶。"""
    _patch_model(monkeypatch)
    view = LiveBarView(
        market_data=_FakeMarketData(),
        today_func=lambda: datetime.date(2024, 1, 3),
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
        (datetime.datetime(2024, 1, 3, 10, 5), datetime.datetime(2024, 1, 3, 10, 0)),
        (datetime.datetime(2024, 1, 3, 14, 57), datetime.datetime(2024, 1, 3, 14, 30)),
        (datetime.datetime(2024, 1, 3, 13, 5), datetime.datetime(2024, 1, 3, 13, 0)),
    ],
)
def test_bucket_start(tm, expected):
    """分钟桶起点按交易时段取整（含午休跳过）。"""
    assert LiveBarView._bucket_start(tm, 30) == expected


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
