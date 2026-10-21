"""参数优化任务管理单元测试。"""

from __future__ import annotations

import datetime
import threading
import time
from unittest.mock import patch

import pytest

from quantide.service.optimization_tasks import (
    OPTIMIZATION_TABLE,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    cancel_optimization,
    get_task,
    list_tasks,
    objective_label,
    start_optimization,
)
from quantide.service.param_optimizer import OptimizationSpec


@pytest.fixture
def opt_db(db, tmp_path):
    """把全局数据库指向独立文件，避免其它测试切换到 :memory: 造成串扰。"""
    path = tmp_path / "optimization.db"
    db.init(path)
    yield db


def _spec(**kwargs) -> OptimizationSpec:
    """构造测试用配置。"""
    defaults = dict(
        strategy_name="demo",
        base_config={"top_k": 5},
        param_grid={"top_k": [3, 5]},
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, 12, 31),
    )
    defaults.update(kwargs)
    return OptimizationSpec(**defaults)


def _wait_task(task_id: str, timeout: float = 5.0) -> dict:
    """等待任务结束并返回状态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(task_id)
        if task and task.get("status") not in {"pending", "running"}:
            return task
        time.sleep(0.05)
    raise AssertionError("任务未在超时内结束")


def test_start_optimization_completes_and_persists(opt_db):
    """任务完成后状态落库。"""
    results = [
        {
            "objective": 1.2,
            "out_objective": 0.4,
            "params": {"top_k": 5},
            "in_sample": {"Sharpe Ratio": "1.20"},
            "out_sample": {"Sharpe Ratio": "0.40"},
        }
    ]
    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.return_value = results
        mock_cls.return_value.cancelled = False
        task_id = start_optimization(_spec())

    task = _wait_task(task_id)
    assert task["status"] == STATUS_DONE
    assert task["results"][0]["params"]["top_k"] == 5
    assert task["results"][0]["overfit"] is False
    stored = opt_db[OPTIMIZATION_TABLE].get(task_id)
    assert stored["status"] == STATUS_DONE
    assert stored["strategy_name"] == "demo"


def test_start_optimization_marks_cancelled(opt_db):
    """被中断的任务状态为 cancelled。"""
    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.return_value = []
        mock_cls.return_value.cancelled = True
        task_id = start_optimization(_spec())

    task = _wait_task(task_id)
    assert task["status"] == STATUS_CANCELLED


def test_start_optimization_marks_failed_on_exception(opt_db):
    """任务异常时状态为 failed 并记录错误。"""
    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.side_effect = RuntimeError("boom")
        task_id = start_optimization(_spec())

    task = _wait_task(task_id)
    assert task["status"] == STATUS_FAILED
    assert "boom" in task["error"]


def test_cancel_optimization_requests_interrupt(opt_db):
    """中断运行中的任务返回 True。"""
    release = []

    def _slow_run(*args, **kwargs):
        release.append(True)
        while not release:
            time.sleep(0.05)
        return []

    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.side_effect = _slow_run
        mock_cls.return_value.cancelled = True
        task_id = start_optimization(_spec())
        assert cancel_optimization(task_id) is True
        release.append(True)

    task = _wait_task(task_id)
    assert task["status"] in {STATUS_CANCELLED, STATUS_DONE}


def test_cancel_unknown_task_returns_false(opt_db):
    """中断不存在的任务返回 False。"""
    assert cancel_optimization("not-a-task") is False


def test_get_task_unknown_returns_none(opt_db):
    """查询不存在的任务返回 None。"""
    assert get_task("not-a-task") is None


def test_list_tasks_filters_by_strategy(opt_db):
    """历史任务按策略过滤。"""
    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.return_value = []
        mock_cls.return_value.cancelled = False
        start_optimization(_spec(strategy_name="alpha"))
        start_optimization(_spec(strategy_name="beta"))

    alpha_tasks = list_tasks("alpha")
    assert alpha_tasks
    assert all(t["strategy_name"] == "alpha" for t in alpha_tasks)


def test_objective_label_maps_chinese_names():
    """目标函数中文名映射。"""
    assert objective_label("sharpe") == "夏普"
    assert objective_label("unknown") == "unknown"


def test_task_progress_callback_updates_running_state(opt_db):
    """进度回调会更新运行中状态。"""
    seen: list[int] = []

    def _run(on_progress=None, **kwargs):
        on_progress(1, 2, {"objective": 0.5, "params": {}})
        return [{"objective": 0.5, "out_objective": 0.4, "params": {}}]

    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.side_effect = _run
        mock_cls.return_value.cancelled = False
        task_id = start_optimization(_spec())

    task = _wait_task(task_id)
    assert task["done"] == 1
    assert task["total"] == 2
    assert seen == []


@pytest.mark.parametrize("objective", ["sharpe", "calmar"])
def test_spec_survives_task_round_trip(opt_db, objective):
    """任务落库后可还原配置。"""
    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.return_value = []
        mock_cls.return_value.cancelled = False
        task_id = start_optimization(_spec(objective=objective))

    _wait_task(task_id)
    task = get_task(task_id)
    assert task["spec"]["objective"] == objective
    assert task["spec"]["param_grid"] == {"top_k": [3, 5]}


def test_running_task_reports_live_progress(opt_db):
    """运行中任务可直接查询到进度。"""
    gate = threading.Event()

    def _run(on_progress=None, **kwargs):
        on_progress(1, 3, {"objective": 0.1, "params": {}})
        gate.wait(5)
        return []

    with patch("quantide.service.optimization_tasks.ParamOptimizer") as mock_cls:
        mock_cls.return_value.run.side_effect = _run
        mock_cls.return_value.cancelled = False
        task_id = start_optimization(_spec())
        time.sleep(0.2)
        live = get_task(task_id)
        gate.set()

    assert live["status"] == "running"
    assert live["done"] == 1
