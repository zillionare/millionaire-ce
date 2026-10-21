"""参数优化：策略参数网格/随机搜索 + 样本内/外切分。

与回测使用完全一致的执行路径（:class:`~quantide.service.runner.BacktestRunner`），
因此优化结果可直接作为回测参数使用。面板与三重障碍标签按内容键缓存，
同一进程内的多个组合可复用，显著降低单组合成本。
"""

from __future__ import annotations

import asyncio
import datetime
import itertools
import random
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from multiprocessing import get_context
from typing import Any

from loguru import logger

from quantide.core.enums import FrameType
from quantide.core.strategy import BaseStrategy
from quantide.data import init_data
from quantide.service.runner import BacktestRunner

OBJECTIVES = ("sharpe", "annual_return", "calmar", "sortino", "max_drawdown")
OBJECTIVE_LABELS = {
    "sharpe": "夏普",
    "annual_return": "年化收益",
    "calmar": "卡玛",
    "sortino": "索提诺",
    "max_drawdown": "最大回撤",
}
#: 目标函数 -> 指标表中的键
OBJECTIVE_METRIC_KEYS = {
    "sharpe": "Sharpe Ratio",
    "annual_return": "CAGR",
    "calmar": "Calmar Ratio",
    "sortino": "Sortino Ratio",
    "max_drawdown": "Max Drawdown",
}
MODES = ("grid", "random")


@dataclass
class OptimizationSpec:
    """一次参数优化的配置。"""

    strategy_name: str
    base_config: dict[str, Any]
    param_grid: dict[str, list[Any]] = field(default_factory=dict)
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    objective: str = "sharpe"
    in_sample_ratio: float = 0.7
    mode: str = "grid"
    n_random: int = 20
    max_workers: int = 4
    random_state: int = 42
    initial_cash: float = 1_000_000
    interval: str = "1d"
    time_budget_minutes: float = 0.0
    data_home: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "strategy_name": self.strategy_name,
            "base_config": dict(self.base_config),
            "param_grid": {k: list(v) for k, v in self.param_grid.items()},
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "objective": self.objective,
            "in_sample_ratio": self.in_sample_ratio,
            "mode": self.mode,
            "n_random": self.n_random,
            "max_workers": self.max_workers,
            "random_state": self.random_state,
            "initial_cash": self.initial_cash,
            "interval": self.interval,
            "time_budget_minutes": self.time_budget_minutes,
            "data_home": self.data_home,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> OptimizationSpec:
        """从字典还原配置。"""
        start = payload.get("start_date")
        end = payload.get("end_date")
        return OptimizationSpec(
            strategy_name=str(payload.get("strategy_name") or ""),
            base_config=dict(payload.get("base_config") or {}),
            param_grid={
                k: list(v) for k, v in (payload.get("param_grid") or {}).items()
            },
            start_date=datetime.date.fromisoformat(start) if start else None,
            end_date=datetime.date.fromisoformat(end) if end else None,
            objective=str(payload.get("objective") or "sharpe"),
            in_sample_ratio=float(payload.get("in_sample_ratio", 0.7)),
            mode=str(payload.get("mode") or "grid"),
            n_random=int(payload.get("n_random", 20)),
            max_workers=int(payload.get("max_workers", 4)),
            random_state=int(payload.get("random_state", 42)),
            initial_cash=float(payload.get("initial_cash", 1_000_000)),
            interval=str(payload.get("interval") or "1d"),
            time_budget_minutes=float(payload.get("time_budget_minutes", 0.0)),
            data_home=str(payload.get("data_home") or ""),
        )


def _split_date(
    start: datetime.date, end: datetime.date, ratio: float
) -> tuple[datetime.date, datetime.date]:
    """按时间顺序切分样本内/样本外。"""
    if not 0.1 <= ratio <= 0.95:
        ratio = 0.7
    total_days = (end - start).days
    cut = start + datetime.timedelta(days=int(total_days * ratio))
    if cut <= start:
        cut = start + datetime.timedelta(days=1)
    if cut >= end:
        cut = end - datetime.timedelta(days=1)
    return cut, end


def parse_metric(value: Any) -> float:
    """解析指标表中的字符串值（如 ``"26.53%"``）为浮点数。"""
    if value is None:
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in {"", "N/A", "-", "nan", "None", "inf"}:
        return float("nan")
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100.0
        return float(text)
    except (TypeError, ValueError):
        return float("nan")


def objective_value(metrics: dict[str, Any], objective: str) -> float:
    """按目标函数从指标表取值（统一为「越大越好」）。"""
    key = OBJECTIVE_METRIC_KEYS.get(objective, OBJECTIVE_METRIC_KEYS["sharpe"])
    value = parse_metric(metrics.get(key))
    if value != value:  # NaN
        return float("-inf")
    if objective == "max_drawdown":
        return -abs(value)
    return value


def _metrics_to_dict(metrics_df: Any) -> dict[str, Any]:
    """把指标表（索引为指标名、列为 Value）转成扁平字典。"""
    if metrics_df is None:
        return {}
    try:
        if hasattr(metrics_df, "columns") and "Value" in metrics_df.columns:
            series = metrics_df["Value"]
            return {str(key): str(value) for key, value in series.items()}
        if hasattr(metrics_df, "to_dict"):
            raw = metrics_df.to_dict()
            if "Value" in raw and isinstance(raw["Value"], dict):
                return {str(k): str(v) for k, v in raw["Value"].items()}
            return {str(k): str(v) for k, v in raw.items()}
    except Exception as exc:  # pragma: no cover - 指标结构异常
        logger.warning(f"指标解析失败: {exc}")
    return {}


def _run_combo(
    strategy_cls: type[BaseStrategy],
    config: dict[str, Any],
    start_date: datetime.date,
    end_date: datetime.date,
    interval: str,
    initial_cash: float,
    in_sample_end: datetime.date,
    objective: str,
    home_dir: str | None = None,
) -> dict[str, Any]:
    """跑一个参数组合，返回样本内/样本外指标。"""
    from quantide.config.settings import get_data_home
    from quantide.service.metrics import metrics as compute_metrics

    data_home = home_dir or get_data_home()
    init_data(data_home, init_db=False)

    runner = BacktestRunner()
    result = asyncio.run(
        runner.run(
            strategy_cls,
            config,
            start_date,
            end_date,
            FrameType(interval),
            initial_cash,
            db_path=":memory:",
        )
    )
    portfolio_id = result.get("portfolio_id")
    row: dict[str, Any] = {"portfolio_id": portfolio_id}
    if not portfolio_id:
        row["error"] = "回测未产出组合"
        return row
    try:
        in_sample = compute_metrics(
            portfolio_id, start=start_date, end=in_sample_end
        )
        out_sample = compute_metrics(
            portfolio_id, start=in_sample_end, end=end_date
        )
        in_dict = _metrics_to_dict(in_sample)
        out_dict = _metrics_to_dict(out_sample)
    except Exception as exc:  # pragma: no cover - 指标计算异常
        logger.warning(f"指标计算失败: {exc}")
        in_dict, out_dict = {}, {}
    row["in_sample"] = {k: str(v) for k, v in in_dict.items()}
    row["out_sample"] = {k: str(v) for k, v in out_dict.items()}
    row["objective"] = objective_value(in_dict, objective)
    row["out_objective"] = objective_value(out_dict, objective)
    return row


def _worker(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """进程池任务入口。"""
    from quantide.config.settings import get_data_home

    spec = OptimizationSpec.from_dict(payload["spec"])
    config = dict(payload["config"])
    strategy_cls = payload.get("strategy_cls")
    if strategy_cls is None:
        from quantide.service.discovery import strategy_loader

        strategies = strategy_loader.load_from_cache()
        strategy_cls = strategies.get(spec.strategy_name)
    if strategy_cls is None:
        return {"error": f"策略不存在: {spec.strategy_name}"}
    assert spec.start_date is not None and spec.end_date is not None
    in_sample_end, _ = _split_date(
        spec.start_date, spec.end_date, spec.in_sample_ratio
    )
    return _run_combo(
        strategy_cls,
        config,
        spec.start_date,
        spec.end_date,
        spec.interval,
        spec.initial_cash,
        in_sample_end,
        spec.objective,
        home_dir=spec.data_home or get_data_home(),
    )


def build_combinations(spec: OptimizationSpec) -> list[dict[str, Any]]:
    """按搜索方式生成参数组合。"""
    base = dict(spec.base_config)
    if spec.mode == "random":
        rng = random.Random(spec.random_state)
        combos: list[dict[str, Any]] = []
        for _ in range(max(1, spec.n_random)):
            config = dict(base)
            for key, values in spec.param_grid.items():
                if values:
                    config[key] = rng.choice(list(values))
            combos.append(config)
        return combos
    keys = list(spec.param_grid)
    values = [list(spec.param_grid[k]) for k in keys]
    combos = []
    for combo in itertools.product(*values) if keys else [()]:
        config = dict(base)
        config.update(dict(zip(keys, combo, strict=True)))
        combos.append(config)
    return combos


def _terminate_workers(executor: Any) -> None:
    """终止仍在运行的 worker 进程（中断或超时时调用）。"""
    for process in list(getattr(executor, "_processes", {}).values()):
        try:
            if process.is_alive():
                process.terminate()
        except Exception:  # pragma: no cover - 进程已退出
            continue


class ParamOptimizer:
    """参数优化执行器（多进程 + 可中断）。"""

    def __init__(self, spec: OptimizationSpec):
        self.spec = spec
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """请求中断优化。"""
        self._cancel.set()

    def _resolve_strategy(self) -> Any:
        """在父进程解析策略类（子进程通过引用导入，避免依赖数据库）。"""
        from quantide.service.discovery import strategy_loader

        strategies = strategy_loader.load_from_cache()
        return strategies.get(self.spec.strategy_name)

    @property
    def cancelled(self) -> bool:
        """是否已被请求中断。"""
        return self._cancel.is_set()

    def run(
        self,
        on_progress: Any = None,
    ) -> list[dict[str, Any]]:
        """执行搜索，返回每个组合的结果。

        Args:
            on_progress: 进度回调 ``(done, total, result)``。

        Returns:
            list[dict]: 每个组合一行结果。
        """
        spec = self.spec
        results: list[dict[str, Any]] = []
        if self._cancel.is_set():
            logger.info("参数优化已被中断")
            return results
        strategy_cls = self._resolve_strategy()
        if strategy_cls is None:
            return [
                {
                    "params": {key: None for key in spec.param_grid},
                    "error": f"策略不存在: {spec.strategy_name}",
                }
            ]
        combinations = build_combinations(spec)
        total = len(combinations)
        if total == 0:
            return results

        deadline = None
        if spec.time_budget_minutes > 0:
            deadline = datetime.datetime.now() + datetime.timedelta(
                minutes=spec.time_budget_minutes
            )

        executor = ProcessPoolExecutor(
            max_workers=max(1, spec.max_workers),
            mp_context=get_context("spawn"),
        )
        stopped = False
        try:
            futures = {}
            for config in combinations:
                if self._cancel.is_set():
                    stopped = True
                    break
                futures[
                    executor.submit(
                        _worker,
                        {
                            "spec": spec.to_dict(),
                            "config": config,
                            "strategy_cls": strategy_cls,
                        },
                    )
                ] = config
            done = 0
            pending = set(futures)
            while pending:
                finished, pending = wait(
                    pending,
                    timeout=1,
                    return_when=FIRST_COMPLETED,
                )
                for future in finished:
                    config = futures[future]
                    done += 1
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.warning(f"参数组合失败 {config}: {exc}")
                        result = {"error": str(exc)}
                    result["params"] = {
                        key: config.get(key) for key in spec.param_grid
                    }
                    result["config"] = config
                    results.append(result)
                    if on_progress is not None:
                        on_progress(done, total, result)
                if self._cancel.is_set():
                    logger.info("参数优化已被中断")
                    stopped = True
                    break
                if deadline is not None and datetime.datetime.now() > deadline:
                    logger.info("参数优化达到时间预算，提前结束")
                    stopped = True
                    break
        finally:
            if stopped:
                _terminate_workers(executor)
            executor.shutdown(wait=not stopped, cancel_futures=stopped)
        return results


def rank_results(
    results: list[dict[str, Any]], objective: str
) -> list[dict[str, Any]]:
    """按目标函数排序，并标记样本外明显劣化（疑似过拟合）。"""
    valid = [r for r in results if not r.get("error")]
    for row in valid:
        in_value = float(row.get("objective") or 0.0)
        out_value = float(row.get("out_objective") or 0.0)
        row["overfit"] = bool(out_value < 0 and in_value > 0)
        row["degradation"] = (in_value - out_value) if in_value else 0.0
    valid.sort(key=lambda r: float(r.get("objective") or 0.0), reverse=True)
    return valid


def new_task_id() -> str:
    """生成任务 ID。"""
    return uuid.uuid4().hex
