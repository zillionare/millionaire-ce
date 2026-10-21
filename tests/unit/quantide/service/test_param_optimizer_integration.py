"""Integration tests for parallel parameter-optimizer worker execution."""

import datetime
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from quantide.service.param_optimizer import OptimizationSpec, ParamOptimizer
from tests.param_optimizer_support import ParamOptimizerRecordingStrategy


@pytest.fixture
def optimizer_env(asset_dir):
    """Set up a temporary environment for optimizer worker processes."""
    tmp_home = tempfile.mkdtemp()
    tmp_path = Path(tmp_home)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)

    shutil.copy(asset_dir / "baseline_calendar.parquet", data_dir / "calendar.parquet")

    stock_list_path = data_dir / "stock_list.parquet"
    if not stock_list_path.exists():
        df = pd.DataFrame(
            {
                "asset": ["000001.SZ"],
                "name": ["PingAn"],
                "list_date": [datetime.date(2000, 1, 1)],
                "delist_date": [None],
            }
        )
        df.to_parquet(stock_list_path)

    bars_dir = tmp_path / "data/bars/daily"
    bars_dir.mkdir(parents=True)
    year_dir = bars_dir / "partition_key_year=2024"
    year_dir.mkdir(parents=True)

    dates = pd.date_range("2024-01-01", "2024-01-31", freq="B")
    df = pd.DataFrame(
        {
            "date": dates,
            "asset": "000001.SZ",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1000.0,
            "amount": 10000.0,
            "adjust": 1.0,
            "turnover": 0.1,
            "is_st": False,
            "up_limit": 11.0,
            "down_limit": 9.0,
        }
    )
    df.to_parquet(year_dir / "data.parquet")

    yield str(tmp_path)

    shutil.rmtree(tmp_home)


def test_optimizer_runs_all_combinations(optimizer_env, db):
    """优化器并行执行所有参数组合并返回逐组合结果。"""
    spec = OptimizationSpec(
        strategy_name="ParamOptimizerRecordingStrategy",
        base_config={"universe": ["000001.SZ"]},
        param_grid={"param1": [1, 2]},
        start_date=datetime.date(2024, 1, 4),
        end_date=datetime.date(2024, 1, 8),
        initial_cash=100000,
        max_workers=2,
        data_home=optimizer_env,
    )

    optimizer = ParamOptimizer(spec)
    with patch.object(
        ParamOptimizer,
        "_resolve_strategy",
        return_value=ParamOptimizerRecordingStrategy,
    ):
        results = optimizer.run()

    assert len(results) == 2
    assert {row["params"]["param1"] for row in results} == {1, 2}
    assert all("error" not in row for row in results)
    assert all(row.get("portfolio_id") for row in results)
    assert all("Sharpe Ratio" in row["in_sample"] for row in results)
    assert all(isinstance(row["out_sample"], dict) for row in results)


def test_optimizer_reports_in_and_out_of_sample_split(optimizer_env, db):
    """样本内与样本外指标来自同一次回测的不同区间。"""
    spec = OptimizationSpec(
        strategy_name="ParamOptimizerRecordingStrategy",
        base_config={"universe": ["000001.SZ"]},
        param_grid={"param1": [1]},
        start_date=datetime.date(2024, 1, 4),
        end_date=datetime.date(2024, 1, 31),
        in_sample_ratio=0.5,
        initial_cash=100000,
        max_workers=1,
        data_home=optimizer_env,
    )

    with patch.object(
        ParamOptimizer,
        "_resolve_strategy",
        return_value=ParamOptimizerRecordingStrategy,
    ):
        results = ParamOptimizer(spec).run()

    assert len(results) == 1
    row = results[0]
    assert row["in_sample"]["Start Date"] <= row["out_sample"]["Start Date"]
    assert row["in_sample"]["End Date"] <= row["out_sample"]["End Date"]
    assert isinstance(row["objective"], float)
    assert isinstance(row["out_objective"], float)
