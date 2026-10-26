"""LiveBarView.get_bars_range 的今日 forming bar 注入测试。"""

from __future__ import annotations

import datetime

import polars as pl

from quantide.service.bar_views import LiveBarView


class _Bars:
    def get_bars_in_range(self, start, end=None, assets=None, adjust=None, eager_mode=True):
        return pl.DataFrame(
            {
                "date": [datetime.date(2024, 1, 2)],
                "asset": ["600519.SH"],
                "open": [10.0],
                "high": [10.2],
                "low": [9.9],
                "close": [10.1],
                "volume": [100.0],
                "amount": [1000.0],
                "adjust": [1.0],
                "is_st": [False],
                "up_limit": [11.0],
                "down_limit": [9.0],
            }
        )


class _MarketData:
    def snapshot(self, symbols):
        return {
            "600519.SH": {
                "price": 10.5,
                "open": 10.0,
                "high": 10.6,
                "low": 9.9,
                "volume": 500.0,
                "amount": 5000.0,
                "prev_close": 10.1,
                "ts": datetime.datetime(2024, 1, 3, 14, 57),
            }
        }


def test_get_bars_range_injects_today_forming_bar_with_date_end():
    view = LiveBarView(
        bars=_Bars(),
        market_data=_MarketData(),
        today_func=lambda: datetime.date(2024, 1, 3),
    )
    frame = view.get_bars_range(
        ["600519.SH"],
        datetime.date(2024, 1, 2),
        datetime.date(2024, 1, 3),
        adjust=None,
        include_forming_bar=True,
    )
    today = frame.filter(pl.col("date") == datetime.date(2024, 1, 3))
    assert today.height == 1
    assert float(today["close"][0]) == 10.5
