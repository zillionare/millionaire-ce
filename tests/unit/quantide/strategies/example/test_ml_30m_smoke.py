from __future__ import annotations

import asyncio
import datetime

import polars as pl
import pytest

pytest.importorskip("lightgbm")

from quantide.core.enums import FrameType
from quantide.strategies.example.ml_30m_smoke import LightGBM30mSmokeStrategy


class _FakeBroker:
    positions: dict[str, object] = {}
    cash = 1_000_000.0

    def get_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        frame_type: FrameType | str = FrameType.DAY,
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "date": [
                    datetime.datetime(2024, 1, 2, 10, 0),
                    datetime.datetime(2024, 1, 2, 10, 30),
                ],
                "close": [10.0, 10.5],
            }
        )


def test_lightgbm_30m_smoke_strategy_scores_completed_bars() -> None:
    strategy = LightGBM30mSmokeStrategy(
        _FakeBroker(),
        {"symbols": ["000001.SZ"], "bar_interval": "30m"},
    )

    asyncio.run(strategy.on_bar(datetime.datetime(2024, 1, 2, 10, 30)))

    assert strategy.BAR_INTERVAL == "30m"
    assert strategy.completed_bars == 1
    assert isinstance(strategy.last_score, float)
