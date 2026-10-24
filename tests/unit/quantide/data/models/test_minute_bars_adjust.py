"""分钟线复权：日线辅助字段缺失时的基准测试。"""

from __future__ import annotations

import datetime

import polars as pl

from quantide.data.models.minute_bars import MinuteBars
from quantide.data.stores.minute_bars import MinuteBarsStore


class _DailyAuxOnlyJan2:
    """模拟日线只同步到 1 月 2 日、分钟线已有 1 月 3 日。"""

    def get_bars_in_range(
        self, start, end=None, assets=None, adjust=None, eager_mode=True
    ):
        return pl.DataFrame(
            {
                "date": [datetime.date(2024, 1, 2)],
                "asset": ["600519.SH"],
                "adjust": [2.0],
                "is_st": [False],
                "up_limit": [22.0],
                "down_limit": [18.0],
            }
        )


def test_qfq_base_uses_forward_filled_factor(monkeypatch, tmp_path, calendar):
    """最新一天缺日线辅助字段时，qfq 基准不能被错置为 1.0。"""
    store = MinuteBarsStore(tmp_path / "minute30", calendar)
    store.append_data(
        pl.DataFrame(
            {
                "date": [
                    datetime.datetime(2024, 1, 2, 10, 0),
                    datetime.datetime(2024, 1, 2, 15, 0),
                    datetime.datetime(2024, 1, 3, 10, 0),
                ],
                "asset": ["600519.SH"] * 3,
                "open": [10.0, 10.0, 11.0],
                "high": [10.0, 10.0, 11.0],
                "low": [10.0, 10.0, 11.0],
                "close": [10.0, 10.0, 11.0],
                "volume": [100.0, 100.0, 100.0],
                "amount": [1000.0, 1000.0, 1000.0],
            }
        ).with_columns(pl.col("date").cast(pl.Datetime("ms")))
    )
    monkeypatch.setattr(
        "quantide.data.models.minute_bars.daily_bars", _DailyAuxOnlyJan2()
    )
    model = MinuteBars().connect(store)
    frame = model.get_bars_in_range(
        datetime.date(2024, 1, 2),
        datetime.date(2024, 1, 3),
        assets=["600519.SH"],
        adjust="qfq",
    )
    rows = frame.sort("date").rows(named=True)
    assert rows[0]["close"] == 10.0
    assert rows[2]["close"] == 11.0
    # 当日日线未同步时涨跌停未知，保持 0（不复用昨日的限价）。
    assert rows[2]["up_limit"] == 0.0
