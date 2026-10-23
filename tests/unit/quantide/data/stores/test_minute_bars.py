"""MinuteBarsStore 与 MinuteBars 读取层的单元测试。"""

from __future__ import annotations

import datetime

import polars as pl
import pytest

from quantide.data.models.calendar import Calendar
from quantide.data.models.minute_bars import MinuteBars
from quantide.data.stores.minute_bars import MinuteBarsStore


@pytest.fixture
def calendar(asset_dir) -> Calendar:
    cal = Calendar()
    cal.load(asset_dir / "baseline_calendar.parquet")
    return cal


def _minute_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [
                datetime.datetime(2024, 1, 2, 9, 30),
                datetime.datetime(2024, 1, 2, 10, 0),
                datetime.datetime(2024, 1, 3, 9, 30),
            ],
            "asset": ["600519.SH"] * 3,
            "open": [10.0, 10.5, 11.0],
            "high": [10.6, 10.7, 11.6],
            "low": [9.9, 10.2, 10.9],
            "close": [10.5, 10.4, 11.5],
            "volume": [100.0, 200.0, 300.0],
            "amount": [1050.0, 2080.0, 3450.0],
        }
    ).with_columns(pl.col("date").cast(pl.Datetime("ms")))


@pytest.mark.parametrize("partition", [None, "year"])
def test_minute_store_roundtrip_and_dedup(tmp_path, calendar, partition):
    """按年分区/单文件都能读写，并按 (date, asset) 去重。"""
    path = tmp_path / ("minute" if partition else "minute.parquet")
    store = MinuteBarsStore(path, calendar)
    store.append_data(_minute_frame())

    frame = store.get(["600519.SH"], datetime.date(2024, 1, 1), None)
    assert len(frame) == 3
    assert frame["date"].dtype == pl.Datetime("ms")

    # 同键重复写入（价格更新）应保留最新
    again = _minute_frame().head(1).with_columns(pl.lit(99.0).alias("close"))
    store.append_data(again)
    frame = store.get(["600519.SH"], datetime.date(2024, 1, 1), None)
    assert len(frame) == 3
    first = frame.sort("date").row(0, named=True)
    assert first["close"] == 99.0


def test_minute_read_applies_qfq_and_limits(tmp_path, calendar, monkeypatch):
    """读取时关联复权因子/ST/涨跌停，并按 qfq 调整价格与限价。"""
    from quantide.data.models import minute_bars as mod

    store = MinuteBarsStore(tmp_path / "minute", calendar)
    store.append_data(_minute_frame())
    model = MinuteBars().connect(store)

    class _FakeDaily:
        def get_bars_in_range(self, start, end, assets=None, adjust=None, eager_mode=True):
            return pl.DataFrame(
                {
                    "date": [datetime.date(2024, 1, 2), datetime.date(2024, 1, 3)],
                    "asset": ["600519.SH", "600519.SH"],
                    "adjust": [1.0, 2.0],
                    "is_st": [False, False],
                    "up_limit": [11.0, 11.5],
                    "down_limit": [9.0, 9.5],
                }
            )

    monkeypatch.setattr(mod, "daily_bars", _FakeDaily())
    frame = model.get_bars_in_range(
        datetime.date(2024, 1, 1), datetime.date(2024, 1, 5), adjust="qfq"
    )
    frame = frame.sort("date")

    # 最新因子 = 2.0：1/2 close 10.5 -> 5.25；1/3 close 11.5 保持
    assert frame["close"][0] == pytest.approx(5.25)
    assert frame["close"][-1] == pytest.approx(11.5)
    # 限价同比例调整：1/2 up_limit 11.0 -> 5.5
    assert frame["up_limit"][0] == pytest.approx(5.5)
    assert frame["down_limit"][0] == pytest.approx(4.5)
    # 成交量反向调整：100 * 2.0 / 1.0 = 200
    assert frame["volume"][0] == pytest.approx(200.0)
