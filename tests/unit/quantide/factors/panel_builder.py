"""因子测试用的合成日线面板构造器。"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl


def _make_synthetic_panel(n_days: int = 65, n_assets: int = 12) -> pl.DataFrame:
    """Build a deterministic multi-board daily-bar panel.

    The first four assets deliberately cover 主板 (600001.SH), 创业板
    (300001.SZ), 科创板 (688001.SH), and 深市中小 (002001.SZ). Prices remain
    positive and vary via a fixed-seed RNG so every assertion is repeatable.
    """
    rng = np.random.default_rng(20260101)
    base_assets = [
        ("600001.SH", 0.10),
        ("300001.SZ", 0.20),
        ("688001.SH", 0.20),
        ("002001.SZ", 0.10),
    ]
    assets: list[tuple[str, float]] = list(base_assets)
    for i in range(len(base_assets), n_assets):
        code, limit_ratio = base_assets[i % len(base_assets)]
        prefix, suffix = code.split(".")
        assets.append((f"{prefix[:3]}{i + 1:03d}.{suffix}", limit_ratio))

    dates: list[dt.date] = []
    cursor = dt.date(2024, 1, 2)
    while len(dates) < n_days:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += dt.timedelta(days=1)

    rows: dict[str, list[object]] = {
        "date": [],
        "asset": [],
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "volume": [],
        "amount": [],
        "adjust": [],
        "is_st": [],
        "up_limit": [],
        "down_limit": [],
    }
    for idx, (asset, limit_ratio) in enumerate(assets):
        close = 8.0 + idx * 1.7 + float(rng.uniform(0.0, 1.5))
        for _day in dates:
            prev_close = close
            close = max(1.0, prev_close * (1.0 + float(rng.normal(0.0015, 0.018))))
            open_price = max(
                1.0, prev_close * (1.0 + float(rng.normal(0.0, 0.007)))
            )
            high = max(open_price, close) * (
                1.0 + abs(float(rng.normal(0.0, 0.009)))
            )
            low = min(open_price, close) * (
                1.0 - abs(float(rng.normal(0.0, 0.009)))
            )
            volume = float(rng.integers(300_000, 2_000_000)) * (1.0 + idx * 0.05)
            rows["date"].append(_day)
            rows["asset"].append(asset)
            rows["open"].append(round(open_price, 2))
            rows["high"].append(round(high, 2))
            rows["low"].append(round(low, 2))
            rows["close"].append(round(close, 2))
            rows["volume"].append(volume)
            rows["amount"].append(round(volume * close, 2))
            rows["adjust"].append(1.0)
            rows["is_st"].append(False)
            rows["up_limit"].append(round(prev_close * (1.0 + limit_ratio), 2))
            rows["down_limit"].append(round(prev_close * (1.0 - limit_ratio), 2))

    return pl.DataFrame(rows).sort(["asset", "date"])


