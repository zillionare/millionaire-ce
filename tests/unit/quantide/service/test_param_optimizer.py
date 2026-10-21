"""参数优化服务单元测试。"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from quantide.service.param_optimizer import (
    OBJECTIVE_METRIC_KEYS,
    OptimizationSpec,
    ParamOptimizer,
    _split_date,
    build_combinations,
    objective_value,
    parse_metric,
    rank_results,
)


def test_spec_round_trip_preserves_fields():
    """配置序列化后可完整还原。"""
    spec = OptimizationSpec(
        strategy_name="demo",
        base_config={"top_k": 5},
        param_grid={"top_k": [3, 5], "train_window": [45, 90]},
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2025, 12, 31),
        objective="calmar",
        in_sample_ratio=0.6,
        mode="random",
        n_random=7,
        max_workers=3,
    )
    restored = OptimizationSpec.from_dict(spec.to_dict())
    assert restored.strategy_name == "demo"
    assert restored.param_grid == {"top_k": [3, 5], "train_window": [45, 90]}
    assert restored.start_date == datetime.date(2024, 1, 1)
    assert restored.end_date == datetime.date(2025, 12, 31)
    assert restored.objective == "calmar"
    assert restored.in_sample_ratio == 0.6
    assert restored.mode == "random"
    assert restored.n_random == 7
    assert restored.max_workers == 3


def test_split_date_orders_in_sample_before_out_sample():
    """样本内区间早于样本外区间。"""
    start = datetime.date(2024, 1, 1)
    end = datetime.date(2026, 1, 1)
    cut, out_end = _split_date(start, end, 0.7)
    assert start < cut < out_end == end


def test_split_date_clamps_invalid_ratio():
    """非法比例回落到默认值。"""
    start = datetime.date(2024, 1, 1)
    end = datetime.date(2026, 1, 1)
    cut, _ = _split_date(start, end, 5.0)
    default_cut, _ = _split_date(start, end, 0.7)
    assert cut == default_cut


def test_parse_metric_handles_percent_and_invalid():
    """百分比与非法值解析正确。"""
    assert parse_metric("26.53%") == pytest.approx(0.2653)
    assert parse_metric("0.88") == pytest.approx(0.88)
    assert parse_metric(1.5) == pytest.approx(1.5)
    assert parse_metric("N/A") != parse_metric("N/A")  # NaN
    assert parse_metric(None) != parse_metric(None)


def test_objective_value_normalizes_to_higher_is_better():
    """最大回撤取负号，其它目标保持原值。"""
    metrics = {
        "Sharpe Ratio": "0.88",
        "CAGR": "26.53%",
        "Calmar Ratio": "0.78",
        "Sortino Ratio": "1.20",
        "Max Drawdown": "-34.03%",
    }
    assert objective_value(metrics, "sharpe") == pytest.approx(0.88)
    assert objective_value(metrics, "annual_return") == pytest.approx(0.2653)
    assert objective_value(metrics, "calmar") == pytest.approx(0.78)
    assert objective_value(metrics, "sortino") == pytest.approx(1.20)
    assert objective_value(metrics, "max_drawdown") == pytest.approx(-0.3403)


def test_objective_value_missing_metric_is_negative_infinity():
    """缺失指标返回负无穷（排序时垫底）。"""
    assert objective_value({}, "sharpe") == float("-inf")


def test_objective_metric_keys_cover_all_objectives():
    """每个目标函数都有对应指标键。"""
    for objective in ("sharpe", "annual_return", "calmar", "sortino", "max_drawdown"):
        assert objective in OBJECTIVE_METRIC_KEYS


def test_build_combinations_grid_is_cartesian_product():
    """网格模式生成笛卡尔积。"""
    spec = OptimizationSpec(
        strategy_name="demo",
        base_config={"fixed": 1},
        param_grid={"a": [1, 2], "b": [3]},
    )
    combos = build_combinations(spec)
    assert len(combos) == 2
    assert all(c["fixed"] == 1 for c in combos)
    assert {c["a"] for c in combos} == {1, 2}


def test_build_combinations_random_is_reproducible():
    """随机模式同 seed 结果一致。"""
    spec = OptimizationSpec(
        strategy_name="demo",
        base_config={},
        param_grid={"a": [1, 2, 3, 4, 5]},
        mode="random",
        n_random=4,
        random_state=7,
    )
    first = build_combinations(spec)
    second = build_combinations(spec)
    assert first == second
    assert len(first) == 4


def test_rank_results_sorts_and_flags_overfit():
    """按样本内目标排序，样本外为负时标记过拟合。"""
    results = [
        {"objective": 0.5, "out_objective": -0.2, "params": {"a": 1}},
        {"objective": 1.2, "out_objective": 0.6, "params": {"a": 2}},
    ]
    ranked = rank_results(results, "sharpe")
    assert [r["params"]["a"] for r in ranked] == [2, 1]
    assert ranked[0]["overfit"] is False
    assert ranked[1]["overfit"] is True


def test_rank_results_drops_error_rows():
    """失败的组合不参与排序。"""
    results = [
        {"error": "boom"},
        {"objective": 0.3, "out_objective": 0.2, "params": {}},
    ]
    ranked = rank_results(results, "sharpe")
    assert len(ranked) == 1


def test_optimizer_cancel_stops_before_running():
    """预先中断时直接返回空结果。"""
    spec = OptimizationSpec(
        strategy_name="demo",
        base_config={},
        param_grid={"a": [1, 2]},
    )
    optimizer = ParamOptimizer(spec)
    optimizer.cancel()
    assert optimizer.cancelled is True
    with patch("quantide.service.param_optimizer.ProcessPoolExecutor") as mock_pool:
        assert optimizer.run() == []
    mock_pool.assert_not_called()


def test_optimizer_run_collects_results_and_params():
    """执行搜索后收集每个组合的参数与结果。"""
    spec = OptimizationSpec(
        strategy_name="demo",
        base_config={},
        param_grid={"a": [1, 2]},
    )
    optimizer = ParamOptimizer(spec)
    progress: list[tuple[int, int]] = []

    class _FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    with patch("quantide.service.param_optimizer.get_context"), patch(
        "quantide.service.param_optimizer.ProcessPoolExecutor"
    ) as mock_pool, patch(
        "quantide.service.param_optimizer.wait",
        side_effect=lambda futures, timeout=None, return_when=None: (
            set(futures),
            set(),
        ),
    ), patch.object(
        ParamOptimizer, "_resolve_strategy", return_value=MagicMock()
    ):
        pool = mock_pool.return_value
        pool.submit.side_effect = lambda fn, payload: _FakeFuture(
            {"objective": payload["config"]["a"]}
        )
        results = optimizer.run(on_progress=lambda d, t, r: progress.append((d, t)))

    assert len(results) == 2
    assert {r["params"]["a"] for r in results} == {1, 2}
    assert progress and progress[0][1] == 2


def test_optimizer_run_handles_future_exception():
    """组合执行异常时记录错误而不中断整体。"""
    spec = OptimizationSpec(
        strategy_name="demo",
        base_config={},
        param_grid={"a": [1]},
    )
    optimizer = ParamOptimizer(spec)

    class _FailingFuture:
        def result(self):
            raise RuntimeError("boom")

    with patch("quantide.service.param_optimizer.get_context"), patch(
        "quantide.service.param_optimizer.ProcessPoolExecutor"
    ) as mock_pool, patch(
        "quantide.service.param_optimizer.wait",
        side_effect=lambda futures, timeout=None, return_when=None: (
            set(futures),
            set(),
        ),
    ), patch.object(
        ParamOptimizer, "_resolve_strategy", return_value=MagicMock()
    ):
        pool = mock_pool.return_value
        pool.submit.return_value = _FailingFuture()
        results = optimizer.run()

    assert len(results) == 1
    assert "boom" in results[0]["error"]


def test_worker_returns_error_for_unknown_strategy():
    """未知策略返回错误信息。"""
    from quantide.service.param_optimizer import _worker

    spec = OptimizationSpec(
        strategy_name="__not_exist__",
        base_config={},
        param_grid={"a": [1]},
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, 6, 30),
    )
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache", return_value={}
    ):
        result = _worker({"spec": spec.to_dict(), "config": {"a": 1}})
    assert "error" in result
    assert "__not_exist__" in result["error"]


def test_run_combo_reports_in_and_out_sample_metrics():
    """单组合返回样本内/样本外指标。"""
    from quantide.service.param_optimizer import _run_combo

    fake_metrics = MagicMock()
    fake_metrics.to_dict.return_value = {"Sharpe Ratio": "1.00", "CAGR": "10.00%"}

    async def _fake_run(*args, **kwargs):
        return {"portfolio_id": "pf-1"}

    with patch("quantide.service.param_optimizer.init_data"), patch(
        "quantide.service.runner.BacktestRunner.run", side_effect=_fake_run
    ), patch("quantide.service.metrics.metrics", return_value=fake_metrics):
        result = _run_combo(
            MagicMock(),
            {},
            datetime.date(2024, 1, 1),
            datetime.date(2024, 12, 31),
            "1d",
            1_000_000,
            datetime.date(2024, 9, 30),
            "sharpe",
        )

    assert result["portfolio_id"] == "pf-1"
    assert result["objective"] == pytest.approx(1.0)
    assert result["out_objective"] == pytest.approx(1.0)
    assert result["in_sample"]["Sharpe Ratio"] == "1.00"


def test_optimizer_cancel_terminates_running_workers():
    """中断请求会终止仍在运行的 worker 并取消未开始的组合。"""
    spec = OptimizationSpec(
        strategy_name="demo",
        base_config={},
        param_grid={"a": [1, 2, 3]},
    )
    optimizer = ParamOptimizer(spec)

    class _FakeFuture:
        def result(self):
            return {"objective": 1.0}

    with patch("quantide.service.param_optimizer.get_context"), patch(
        "quantide.service.param_optimizer.ProcessPoolExecutor"
    ) as mock_pool, patch(
        "quantide.service.param_optimizer.wait",
        side_effect=lambda futures, timeout=None, return_when=None: (
            set(futures),
            set(),
        ),
    ), patch.object(
        ParamOptimizer, "_resolve_strategy", return_value=MagicMock()
    ):
        pool = mock_pool.return_value
        pool.submit.return_value = _FakeFuture()
        optimizer.run(on_progress=lambda d, t, r: optimizer.cancel())

    assert optimizer.cancelled is True
    assert pool.shutdown.call_args.kwargs == {
        "wait": False,
        "cancel_futures": True,
    }
