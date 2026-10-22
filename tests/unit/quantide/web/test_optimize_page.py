"""参数优化页面单元测试。"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from quantide.web.pages import optimize as optimize_page_mod


def _make_req(query: dict | None = None) -> MagicMock:
    """构造带查询参数的请求替身。"""
    req = MagicMock()
    req.query_params = query or {}
    return req


def _fake_strategy() -> type:
    """构造带 PARAMS 的策略替身。"""
    class _FakeStrategy:
        PARAMS = {"top_k": 5, "train_window": 45, "horizon": 3}

    return _FakeStrategy


def test_page_renders_config_form():
    """页面渲染出配置表单与参数输入。"""
    with patch.object(
        optimize_page_mod,
        "_build_spec",
        side_effect=AssertionError("不应构造配置"),
    ), patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ), patch(
        "quantide.web.pages.optimize.list_tasks", return_value=[]
    ):
        html = optimize_page_mod.to_xml(
            optimize_page_mod.optimize_page(_make_req(), {}, "demo")
        )
    assert "参数优化" in html
    assert "param_top_k" in html
    assert "objective" in html
    assert "in_sample_ratio" in html


def test_page_redirects_for_unknown_strategy():
    """未知策略重定向回列表。"""
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={},
    ):
        resp = optimize_page_mod.optimize_page(_make_req(), {}, "nope")
    assert getattr(resp, "status_code", None) == 307


def test_build_spec_parses_grid_and_base_params():
    """表单解析出参数网格与固定参数。"""
    form = {
        "param_top_k": "3, 5, 8",
        "param_train_window": "45",
        "param_horizon": "3",
        "start_date": "2024-01-01",
        "end_date": "2025-12-31",
        "objective": "calmar",
        "mode": "grid",
        "in_sample_ratio": "0.6",
        "max_workers": "3",
        "n_random": "10",
        "time_budget_minutes": "30",
        "initial_cash": "500000",
    }
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ):
        spec = optimize_page_mod._build_spec("demo", form)

    assert spec.param_grid == {"top_k": [3, 5, 8]}
    assert spec.base_config["train_window"] == 45
    assert spec.objective == "calmar"
    assert spec.in_sample_ratio == 0.6
    assert spec.max_workers == 3
    assert spec.time_budget_minutes == 30
    assert spec.initial_cash == 500000
    assert spec.start_date == datetime.date(2024, 1, 1)


def test_build_spec_falls_back_on_invalid_values():
    """非法取值回落到默认值。"""
    form = {
        "param_top_k": "abc",
        "start_date": "2024-01-01",
        "end_date": "2025-12-31",
        "objective": "not_exist",
        "mode": "not_exist",
        "in_sample_ratio": "x",
        "max_workers": "y",
    }
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ):
        spec = optimize_page_mod._build_spec("demo", form)

    assert spec.objective == "sharpe"
    assert spec.mode == "grid"
    assert spec.in_sample_ratio == 0.7
    assert spec.max_workers == 4


@pytest.mark.asyncio
async def test_run_route_rejects_empty_grid():
    """没有可搜索参数时给出提示。"""
    req = MagicMock()

    async def _form():
        return {
            "param_top_k": "5",
            "start_date": "2024-01-01",
            "end_date": "2025-12-31",
        }

    req.form = _form
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ):
        html = optimize_page_mod.to_xml(
            await optimize_page_mod.run_optimize(req, "demo")
        )
    assert "多个取值" in html


@pytest.mark.asyncio
async def test_run_route_starts_task_and_renders_panel():
    """启动任务后渲染进度面板。"""
    req = MagicMock()

    async def _form():
        return {
            "param_top_k": "3,5",
            "start_date": "2024-01-01",
            "end_date": "2025-12-31",
        }

    req.form = _form
    task = {
        "task_id": "t1",
        "status": "running",
        "objective": "sharpe",
        "total": 2,
        "done": 1,
        "results": [],
        "spec": {"start_date": "2024-01-01", "end_date": "2025-12-31"},
    }
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ), patch(
        "quantide.web.pages.optimize.start_optimization", return_value="t1"
    ) as mock_start, patch(
        "quantide.web.pages.optimize.get_task", return_value=task
    ):
        html = optimize_page_mod.to_xml(
            await optimize_page_mod.run_optimize(req, "demo")
        )
    assert mock_start.called
    assert "进行中" in html
    assert "中断任务" in html


def test_results_table_shows_overfit_warning_and_prefill():
    """结果表展示样本内外指标与过拟合告警。"""
    task = {
        "task_id": "t1",
        "status": "done",
        "objective": "sharpe",
        "total": 2,
        "done": 2,
        "spec": {"start_date": "2024-01-01", "end_date": "2025-12-31"},
        "results": [
            {
                "objective": 1.2,
                "out_objective": -0.3,
                "params": {"top_k": 5},
                "in_sample": {"CAGR": "26.53%", "Max Drawdown": "-34.03%"},
                "out_sample": {"CAGR": "-4.00%", "Max Drawdown": "-20.00%"},
                "overfit": True,
            }
        ],
    }
    with patch("quantide.web.pages.optimize.get_task", return_value=task):
        html = optimize_page_mod.to_xml(
            optimize_page_mod._render_task_panel("demo", "t1")
        )
    assert "疑似过拟合" in html
    assert "26.53%" in html
    assert "prefill=" in html
    assert "top_k=5" in html


def test_cancel_route_calls_cancel_and_renders():
    """中断路由触发取消并回渲染。"""
    task = {
        "task_id": "t1",
        "status": "cancelled",
        "objective": "sharpe",
        "total": 2,
        "done": 1,
        "results": [],
        "spec": {},
    }
    with patch(
        "quantide.web.pages.optimize.cancel_optimization"
    ) as mock_cancel, patch(
        "quantide.web.pages.optimize.get_task", return_value=task
    ):
        html = optimize_page_mod.to_xml(
            optimize_page_mod.optimize_cancel("demo", "t1")
        )
    assert mock_cancel.called
    assert "中断" in html


def test_encode_prefill_round_trip():
    """预填参数编码可逆。"""
    payload = {"params": {"top_k": 5}, "start_date": "2024-01-01"}
    encoded = optimize_page_mod._encode_prefill(payload)
    import base64
    import json

    decoded = json.loads(base64.urlsafe_b64decode(encoded + "==").decode("utf-8"))
    assert decoded["params"]["top_k"] == 5


def test_parse_param_values_converts_types():
    """取值解析为数值或字符串。"""
    assert optimize_page_mod._parse_param_values("1, 2.5, x") == [1, 2.5, "x"]
    assert optimize_page_mod._parse_param_values("") == []


@pytest.mark.parametrize(
    "value,expected",
    [("0.5", "50.00%"), ("-0.25", "-25.00%"), ("bad", "-")],
)
def test_format_pct(value, expected):
    """百分比格式化。"""
    assert optimize_page_mod._format_pct(value) == expected


def test_history_table_lists_tasks():
    """历史任务表渲染任务信息。"""
    tasks = [
        {
            "task_id": "t1",
            "started_at": "2024-05-01T10:00:00",
            "status": "done",
            "objective": "sharpe",
            "done": 3,
            "total": 3,
            "spec": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        }
    ]
    with patch(
        "quantide.web.pages.optimize.list_tasks", return_value=tasks
    ):
        html = optimize_page_mod.to_xml(optimize_page_mod._render_history("demo"))
    assert "2024-05-01" in html
    assert "夏普" in html


def test_page_renders_existing_task_from_query():
    """带 task_id 访问时渲染对应任务结果。"""
    task = {
        "task_id": "t1",
        "status": "done",
        "objective": "sharpe",
        "total": 1,
        "done": 1,
        "results": [],
        "spec": {},
    }
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ), patch(
        "quantide.web.pages.optimize.list_tasks", return_value=[]
    ), patch("quantide.web.pages.optimize.get_task", return_value=task):
        html = optimize_page_mod.to_xml(
            optimize_page_mod.optimize_page(
                _make_req({"task_id": "t1"}), {}, "demo"
            )
        )
    assert "已完成" in html


def test_objective_label_helper_exposed():
    """目标函数中文名可在页面使用。"""
    assert optimize_page_mod.objective_label("sharpe") == "夏普"


def test_session_absent_does_not_break_page():
    """Session 缺字段时页面仍可渲染。"""
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ), patch(
        "quantide.web.pages.optimize.list_tasks", return_value=[]
    ):
        html = optimize_page_mod.to_xml(
            optimize_page_mod.optimize_page(_make_req(), {}, "demo")
        )
    assert "参数优化" in html


def test_backtest_modal_prefills_params_and_dates(monkeypatch):
    """参数优化结果可回填回测向导（参数与日期）。"""
    from quantide.web.pages import strategy as strategy_mod

    prefill = optimize_page_mod._encode_prefill(
        {
            "params": {"top_k": 3},
            "start_date": "2024-01-02",
            "end_date": "2024-03-29",
        }
    )
    req = MagicMock()
    req.query_params = {"prefill": prefill}
    with patch.object(
        strategy_mod.strategy_loader,
        "load_from_cache",
        return_value={"demo": _fake_strategy()},
    ):
        html = strategy_mod.to_xml(strategy_mod.backtest_modal("demo", req))
    assert 'name="param_top_k"' in html
    assert 'value="3"' in html
    assert 'value="2024-01-02"' in html
    assert 'value="2024-03-29"' in html


def test_backtest_modal_ignores_invalid_prefill(monkeypatch):
    """非法 prefill 不影响回测向导渲染。"""
    from quantide.web.pages import strategy as strategy_mod

    req = MagicMock()
    req.query_params = {"prefill": "not-base64!!"}
    with patch.object(
        strategy_mod.strategy_loader,
        "load_from_cache",
        return_value={"demo": _fake_strategy()},
    ):
        html = strategy_mod.to_xml(strategy_mod.backtest_modal("demo", req))
    assert 'name="param_top_k"' in html
    assert "2024-01-01" in html


def test_drawdown_objective_shows_percentage_and_direction():
    """回撤类目标展示为百分比并标注方向。"""
    task = {
        "task_id": "t1",
        "status": "done",
        "objective": "max_drawdown",
        "total": 1,
        "done": 1,
        "spec": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        "results": [
            {
                "objective": -0.3403,
                "out_objective": -0.2,
                "params": {"top_k": 5},
                "in_sample": {"CAGR": "26.53%", "Max Drawdown": "-34.03%"},
                "out_sample": {"CAGR": "10.00%", "Max Drawdown": "-20.00%"},
                "overfit": True,
            }
        ],
    }
    with patch("quantide.web.pages.optimize.get_task", return_value=task):
        html = optimize_page_mod.to_xml(
            optimize_page_mod._render_task_panel("demo", "t1")
        )
    assert "越小越好" in html
    assert "-34.03%" in html


def _task_with_monthly() -> dict:
    """构造带月度分解的任务数据。"""
    return {
        "task_id": "t1",
        "status": "done",
        "objective": "sharpe",
        "total": 2,
        "done": 2,
        "spec": {"start_date": "2024-01-02", "end_date": "2024-03-29"},
        "results": [
            {
                "objective": 1.2,
                "out_objective": 0.3,
                "params": {"train_window": 45},
                "in_sample": {},
                "out_sample": {},
                "overfit": False,
                "monthly": [
                    {"month": "2024-01", "ret": 0.05, "partial": False},
                    {"month": "2024-02", "ret": -0.02, "partial": False},
                    {"month": "2024-03", "ret": 0.01, "partial": True},
                ],
            },
            {
                "objective": 0.8,
                "out_objective": 0.2,
                "params": {"train_window": 120},
                "in_sample": {},
                "out_sample": {},
                "overfit": False,
                "monthly": [
                    {"month": "2024-01", "ret": 0.02, "partial": False},
                    {"month": "2024-02", "ret": 0.03, "partial": False},
                    {"month": "2024-03", "ret": -0.04, "partial": True},
                ],
            },
        ],
    }


def test_monthly_matrix_renders_months_and_summary():
    """月度分解表渲染出月份列与行尾统计。"""
    html = optimize_page_mod.to_xml(
        optimize_page_mod._render_monthly_matrix(_task_with_monthly())
    )
    assert "月度分解" in html
    assert "2024-01" in html and "2024-03" in html
    assert "+5.0%" in html and "-2.0%" in html
    # 行尾统计：正收益月数 / 最差月 / 中位
    assert "2/3" in html
    assert "-4.0%" in html


def test_monthly_matrix_marks_partial_month():
    """未满月带星号标记。"""
    html = optimize_page_mod.to_xml(
        optimize_page_mod._render_monthly_matrix(_task_with_monthly())
    )
    assert "+1.0%*" in html


def test_monthly_matrix_highlights_best_cell_per_month():
    """每列最优的格子加粗。"""
    html = optimize_page_mod.to_xml(
        optimize_page_mod._render_monthly_matrix(_task_with_monthly())
    )
    assert "font-bold" in html


def test_monthly_matrix_empty_without_monthly_data():
    """没有月度数据时不渲染表格。"""
    task = _task_with_monthly()
    for row in task["results"]:
        row.pop("monthly")
    html = optimize_page_mod.to_xml(
        optimize_page_mod._render_monthly_matrix(task)
    )
    assert "月度分解" not in html


def test_fresh_page_contains_run_button_and_its_target():
    """首次进入页面时「开始优化」按钮与其 HTMX 目标必须同时存在。

    回归测试：目标容器缺失会让点击按钮毫无反应。
    """
    with patch(
        "quantide.service.discovery.strategy_loader.load_from_cache",
        return_value={"demo": _fake_strategy()},
    ), patch(
        "quantide.web.pages.optimize.list_tasks", return_value=[]
    ):
        html = optimize_page_mod.to_xml(
            optimize_page_mod.optimize_page(_make_req(), {}, "demo")
        )
    assert "开始优化" in html
    assert 'id="optimize-task-wrapper"' in html
    assert 'hx-target="#optimize-task-wrapper"' in html
