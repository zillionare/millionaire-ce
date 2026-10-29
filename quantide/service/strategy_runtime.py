import asyncio
import datetime
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from quantide.config.features import require_live_trading, supports_live_trading
from quantide.config.paths import get_backtest_log_path, get_strategy_runtime_state_path
from quantide.config.settings import get_cheat_on_close_time
from quantide.core.enums import BrokerKind, FrameType
from quantide.core.ports.broker import ExecutionResult
from quantide.core.runtime import RuntimeContext
from quantide.core.runtime.registration import register_port_backed_broker
from quantide.data.models.calendar import calendar
from quantide.data.models.daily_bars import daily_bars
from quantide.data.sqlite import db
from quantide.service.discovery import strategy_loader
from quantide.service.registry import BrokerRegistry
from quantide.service.risk_programs import build_live_risk_strategies
from quantide.service.sim_broker import PaperBroker
from quantide.service.strategy_tracking import tracking_store


def _optional_float(value: Any) -> float | None:
    """把配置值转成 float；空值/非法值返回 None（保留 broker 默认）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_or(value: Any, default: float) -> float:
    parsed = _optional_float(value)
    return parsed if parsed is not None else default


@dataclass
class StrategyRuntime:
    runtime_id: str
    mode: str
    strategy_name: str
    strategy_id: str
    portfolio_id: str
    account_kind: str
    status: str
    config: dict[str, Any]
    interval: str = "1d"
    source_backtest_portfolio_id: str = ""
    symbols: list[str] = field(default_factory=list)
    principal: float = 0.0
    started_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    updated_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    error: str = ""
    stop_event: threading.Event | None = None
    thread: threading.Thread | None = None
    broker: Any = None


@dataclass
class BacktestRun:
    runtime_id: str
    portfolio_id: str
    strategy_name: str
    config: dict[str, Any]
    interval: str
    start_date: str
    end_date: str
    initial_cash: float
    status: str
    save_logs: bool = False
    log_path: str = ""
    cheat_on_close: bool = False
    cheat_on_close_time: str = ""
    created_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    updated_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    error: str = ""


class StrategyBrokerProxy:
    def __init__(self, broker: Any, strategy_id: str):
        self._broker = broker
        self._strategy_id = strategy_id

    def __getattr__(self, item):
        return getattr(self._broker, item)

    async def buy(self, asset, shares, price=0, order_time=None, timeout=0.5, **kwargs):
        kwargs.update(
            asset=asset,
            shares=shares,
            price=price,
            order_time=order_time,
            timeout=timeout,
            strategy_id=self._strategy_id,
        )
        return await self._broker.buy(
            **kwargs,
        )

    async def sell(self, asset, shares, price=0, order_time=None, timeout=0.5, **kwargs):
        kwargs.update(
            asset=asset,
            shares=shares,
            price=price,
            order_time=order_time,
            timeout=timeout,
            strategy_id=self._strategy_id,
        )
        return await self._broker.sell(
            **kwargs,
        )

    async def buy_percent(self, asset, percent, price=0, order_time=None, timeout=0.5, **kwargs):
        kwargs.update(
            asset=asset,
            percent=percent,
            price=price,
            order_time=order_time,
            timeout=timeout,
            strategy_id=self._strategy_id,
        )
        return await self._broker.buy_percent(
            **kwargs,
        )

    async def sell_percent(self, asset, percent, price=0, order_time=None, timeout=0.5, **kwargs):
        kwargs.update(
            asset=asset,
            percent=percent,
            price=price,
            order_time=order_time,
            timeout=timeout,
            strategy_id=self._strategy_id,
        )
        return await self._broker.sell_percent(
            **kwargs,
        )

    async def buy_amount(self, asset, amount, price=0, order_time=None, timeout=0.5, **kwargs):
        kwargs.update(
            asset=asset,
            amount=amount,
            price=price,
            order_time=order_time,
            timeout=timeout,
            strategy_id=self._strategy_id,
        )
        return await self._broker.buy_amount(
            **kwargs,
        )

    async def sell_amount(self, asset, amount, price=0, order_time=None, timeout=0.5, **kwargs):
        kwargs.update(
            asset=asset,
            amount=amount,
            price=price,
            order_time=order_time,
            timeout=timeout,
            strategy_id=self._strategy_id,
        )
        return await self._broker.sell_amount(
            **kwargs,
        )

    async def trade_target_pct(
        self, asset, target_pct, price=0, order_time=None, timeout=0.5, **kwargs
    ):
        kwargs.update(
            asset=asset,
            target_pct=target_pct,
            price=price,
            order_time=order_time,
            timeout=timeout,
            strategy_id=self._strategy_id,
        )
        return await self._broker.trade_target_pct(**kwargs)


class NextOpenBrokerProxy:
    """Turn strategy orders into durable next-trading-day-open intents."""

    _ORDER_METHODS = {
        "buy",
        "sell",
        "buy_percent",
        "sell_percent",
        "buy_amount",
        "sell_amount",
        "trade_target_pct",
    }

    def __init__(
        self,
        broker: Any,
        *,
        runtime_id: str,
        portfolio_id: str,
        strategy_id: str,
        signal_date: datetime.date,
    ) -> None:
        self._broker = broker
        self._runtime_id = runtime_id
        self._portfolio_id = portfolio_id
        self._strategy_id = strategy_id
        self._signal_date = signal_date

    def __getattr__(self, item: str) -> Any:
        return getattr(self._broker, item)

    def _target_date(self) -> datetime.date:
        try:
            return calendar.day_shift(self._signal_date, 1)
        except Exception:
            return self._signal_date + datetime.timedelta(days=1)

    def _enqueue(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]):
        key_payload = {
            "method": method,
            "args": list(args),
            # order_time identifies when the caller happened to retry, not
            # the order intent.  Excluding it makes persistence idempotent
            # for keyword and positional calls alike.
            "kwargs": {key: value for key, value in kwargs.items() if key != "order_time"},
            "signal_date": self._signal_date.isoformat(),
        }
        intent_key = json.dumps(key_payload, sort_keys=True, default=str)
        tracking_store.enqueue_next_open_intent(
            runtime_id=self._runtime_id,
            portfolio_id=self._portfolio_id,
            strategy_id=self._strategy_id,
            signal_date=self._signal_date,
            target_date=self._target_date(),
            method=method,
            args=list(args),
            kwargs=kwargs,
            intent_key=intent_key,
        )
        return ExecutionResult.empty()

    def __getattribute__(self, item: str) -> Any:
        if item in object.__getattribute__(self, "_ORDER_METHODS"):
            async def queued(*args, **kwargs):
                return object.__getattribute__(self, "_enqueue")(item, args, kwargs)

            return queued
        return object.__getattribute__(self, item)


class NextOpenBarBrokerProxy:
    """Persist 30m strategy orders until the next eligible bar boundary."""

    _ORDER_METHODS = {
        "buy",
        "sell",
        "buy_percent",
        "sell_percent",
        "buy_amount",
        "sell_amount",
        "trade_target_pct",
    }

    def __init__(
        self,
        broker: Any,
        *,
        runtime_id: str,
        portfolio_id: str,
        strategy_id: str,
        signal_bar: datetime.datetime,
    ) -> None:
        self._broker = broker
        self._runtime_id = runtime_id
        self._portfolio_id = portfolio_id
        self._strategy_id = strategy_id
        self._signal_bar = signal_bar.replace(tzinfo=None)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._broker, item)

    def _target_bar(self) -> datetime.datetime:
        try:
            next_completed = calendar.shift(self._signal_bar, 1, FrameType.MIN30)
        except Exception as exc:
            raise RuntimeError(
                "无法确定 30m next_open 的下一个交易时段，拒绝以内存队列下单"
            ) from exc
        if not isinstance(next_completed, datetime.datetime):
            raise RuntimeError("30m next_open target bar 无效，拒绝下单")
        # Calendar minute frames are bar-end timestamps: 13:30 is the end of
        # the first afternoon bar, whose executable next-open is 13:00.
        return (next_completed - datetime.timedelta(minutes=30)).replace(tzinfo=None)

    def _enqueue(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]):
        target_bar = self._target_bar()
        key_payload = {
            "interval": "30m",
            "method": method,
            "args": list(args),
            # order_time identifies a retry, not the order intent.
            "kwargs": {key: value for key, value in kwargs.items() if key != "order_time"},
            "signal_bar": self._signal_bar.isoformat(timespec="seconds"),
            "target_bar": target_bar.isoformat(timespec="seconds"),
        }
        intent_key = json.dumps(key_payload, sort_keys=True, default=str)
        tracking_store.enqueue_next_open_intent(
            runtime_id=self._runtime_id,
            portfolio_id=self._portfolio_id,
            strategy_id=self._strategy_id,
            signal_date=self._signal_bar.date(),
            target_date=target_bar.date(),
            signal_bar=self._signal_bar,
            target_bar=target_bar,
            method=method,
            args=list(args),
            kwargs=kwargs,
            intent_key=intent_key,
        )
        return ExecutionResult.empty()

    def __getattribute__(self, item: str) -> Any:
        if item in object.__getattribute__(self, "_ORDER_METHODS"):
            async def queued(*args, **kwargs):
                return object.__getattribute__(self, "_enqueue")(item, args, kwargs)

            return queued
        return object.__getattribute__(self, item)


class StrategyRuntimeManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._account_runtimes: dict[str, StrategyRuntime] = {}
        self._strategy_runtimes: dict[str, StrategyRuntime] = {}
        self._backtest_runtimes: dict[str, BacktestRun] = {}
        self._backtest_history: dict[str, BacktestRun] = {}
        self._runtime_specs: dict[str, dict[str, Any]] = {}
        self._blocked_accounts: dict[str, dict[str, Any]] = {}
        self._blocked_strategies: dict[str, dict[str, Any]] = {}
        self._risk_events: list[dict[str, Any]] = []
        #: 已发出“策略行情缺失”告警的 runtime，用于恢复时消警
        self._data_missing_alarms: set[str] = set()
        self._runtime: RuntimeContext | None = None
        self._registry: BrokerRegistry | None = None
        self._adapters: Any = None
        self._market_data: Any = None
        self._gateway_broker: Any = None

    def bootstrap_from_runtime(self, runtime: RuntimeContext) -> None:
        """使用正式 RuntimeContext 初始化运行时管理器。"""
        self._runtime = runtime
        self._registry = runtime.registry
        self._adapters = runtime.adapters
        self._market_data = runtime.market_data
        self._gateway_broker = runtime.registry.get(BrokerKind.QMT, "gateway")
        registry = runtime.registry
        with self._lock:
            self._account_runtimes = {}
            for item in registry.list():
                kind = item["kind"]
                portfolio_id = item["id"]
                mode = "live" if kind == BrokerKind.QMT.value else "paper"
                runtime_id = f"{mode}:{portfolio_id}"
                self._account_runtimes[runtime_id] = StrategyRuntime(
                    runtime_id=runtime_id,
                    mode=mode,
                    strategy_name="",
                    strategy_id="",
                    portfolio_id=portfolio_id,
                    account_kind=kind,
                    status="idle",
                    config={},
                    broker=registry.get(kind, portfolio_id),
                )
        self._load_specs()
        self._restore_persisted_runtimes()

    def create_backtest_runtime(
        self,
        portfolio_id: str,
        strategy_name: str,
        config: dict[str, Any],
        interval: str,
        start_date: str,
        end_date: str,
        initial_cash: float,
        save_logs: bool = False,
    ) -> None:
        if interval not in {"1d", "30m"}:
            raise ValueError(f"不支持的策略周期: {interval!r}")
        strategy_cls = strategy_loader.load_from_cache().get(strategy_name)
        config = dict(config or {})
        if "risk_programs" in config:
            from quantide.service.risk_programs import normalize_risk_programs

            config["risk_programs"] = normalize_risk_programs(
                config["risk_programs"],
                host_strategy_cls=strategy_cls,
                frame_type=interval,
            )
        if strategy_cls is not None:
            from quantide.service.runner import BacktestRunner

            BacktestRunner.validate_strategy_interval(
                strategy_cls, config, interval
            )
        cheat = bool(config.get("cheat_on_close", False))
        cheat_tm = get_cheat_on_close_time() if cheat else ""
        run = BacktestRun(
            runtime_id=f"backtest:{portfolio_id}",
            portfolio_id=portfolio_id,
            strategy_name=strategy_name,
            config=config,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            initial_cash=initial_cash,
            status="running",
            save_logs=save_logs,
            log_path=str(get_backtest_log_path(portfolio_id)),
            cheat_on_close=cheat,
            cheat_on_close_time=cheat_tm,
        )
        with self._lock:
            self._backtest_runtimes[portfolio_id] = run
            self._backtest_history[portfolio_id] = run
            self._runtime_specs[run.runtime_id] = {
                "runtime_id": run.runtime_id,
                "mode": "backtest",
                "strategy_name": strategy_name,
                "strategy_id": "",
                "portfolio_id": portfolio_id,
                "source_backtest_portfolio_id": "",
                "account_kind": "bt",
                "status": run.status,
                "config": dict(config or {}),
                "symbols": [],
                "principal": initial_cash,
                "interval": interval,
                "cheat_on_close": cheat,
                "cheat_on_close_time": cheat_tm,
            }
            self._save_specs()

    def complete_backtest_runtime(self, portfolio_id: str, error: str = "") -> None:
        with self._lock:
            run = self._backtest_history.get(portfolio_id)
            if run is not None:
                run.status = "failed" if error else "finished"
                run.error = error
                run.updated_at = datetime.datetime.now()
                spec = self._runtime_specs.get(run.runtime_id)
                if spec is not None:
                    spec["status"] = run.status
                    spec["error"] = run.error
                    self._save_specs()
            if portfolio_id in self._backtest_runtimes:
                del self._backtest_runtimes[portfolio_id]
        if error:
            self.record_system_event(
                title="回测运行失败",
                message=f"回测 {portfolio_id} 失败：{error}",
                severity="critical",
                blocked=True,
                target_id=f"backtest:{portfolio_id}",
            )

    def get_backtest_run(self, portfolio_id: str) -> BacktestRun | None:
        with self._lock:
            return self._backtest_history.get(portfolio_id)

    def get_backtest_run_or_resolve(self, portfolio_id: str) -> BacktestRun | None:
        """优先 in-memory history, 缺失时回退 _resolve_backtest_run (持久化恢复).

        #47/#49 followup2: 进程重启后 _backtest_history 为空, 报告页 / deploy modal
        仍需拿到恢复后的 run 才能正常渲染. 回退失败 (portfolio 不存在) 时返 None.
        """
        run = self.get_backtest_run(portfolio_id)
        if run is not None:
            return run
        try:
            return self._resolve_backtest_run(portfolio_id)
        except Exception:
            return None

    def remove_backtest_run(self, portfolio_id: str) -> None:
        """从内存中移除指定回测的运行记录。

        Args:
            portfolio_id: 组合 ID。
        """
        with self._lock:
            self._backtest_history.pop(portfolio_id, None)
            self._backtest_runtimes.pop(portfolio_id, None)

    def deploy_to_paper(
        self,
        portfolio_id: str,
        principal: float,
        registry: BrokerRegistry,
        market_data: Any,
        config: dict[str, Any] | None = None,
    ) -> StrategyRuntime:
        run = self._resolve_backtest_run(portfolio_id)
        if run.status == "failed":
            raise RuntimeError(f"回测不可投放：{run.error or '回测已失败'}")
        existing = self.get_active_backtest_deployment(run.portfolio_id, "paper")
        if existing is not None:
            return existing
        effective_config = config if config is not None else run.config
        self._validate_config_interval(effective_config, run.interval)
        account_id = f"paper-{run.strategy_name}-{uuid.uuid4().hex[:8]}"
        config = effective_config or {}
        broker = PaperBroker.create(
            portfolio_id=account_id,
            portfolio_name=f"{run.strategy_name}-paper",
            principal=principal,
            commission=_float_or(config.get("commission"), 1e-4),
            stamp_tax=_float_or(config.get("stamp_duty"), 5e-4),
            transfer_fee=_float_or(config.get("transfer_fee"), 1e-5),
            min_commission=_float_or(config.get("min_commission"), 5.0),
            slippage=_float_or(config.get("slippage_pct"), 0.001),
            market_data=market_data,
        )
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("runtime 未初始化")
        port = broker
        handle = register_port_backed_broker(
            registry=runtime.registry,
            adapters=runtime.adapters,
            port=port,
            portfolio_id=account_id,
            kind=BrokerKind.SIMULATION,
            portfolio_name=f"{run.strategy_name}-paper",
            status=True,
            is_connected=True,
        )
        return self._start_strategy_runtime(
            mode="paper",
            strategy_name=run.strategy_name,
            config=effective_config,
            broker=handle,
            portfolio_id=account_id,
            source_backtest_portfolio_id=run.portfolio_id,
            account_kind=BrokerKind.SIMULATION.value,
            interval=run.interval,
            market_data=market_data,
            principal=principal,
        )

    def deploy_to_live(
        self,
        portfolio_id: str,
        account_id: str,
        registry: BrokerRegistry,
        market_data: Any,
        config: dict[str, Any] | None = None,
    ) -> StrategyRuntime:
        require_live_trading()
        run = self._resolve_backtest_run(portfolio_id)
        if run.status == "failed":
            raise RuntimeError(f"回测不可投放：{run.error or '回测已失败'}")
        existing = self.get_active_backtest_deployment(run.portfolio_id, "live")
        if existing is not None:
            return existing
        effective_config = config if config is not None else run.config
        self._validate_config_interval(effective_config, run.interval)
        broker = self._gateway_broker
        account_kind = "gateway"
        account_id = "gateway"
        if broker is None:
            raise RuntimeError("gateway broker 未初始化")
        return self._start_strategy_runtime(
            mode="live",
            strategy_name=run.strategy_name,
            config=effective_config,
            broker=broker,
            portfolio_id=account_id,
            source_backtest_portfolio_id=run.portfolio_id,
            account_kind=account_kind,
            interval=run.interval,
            market_data=market_data,
            principal=0.0,
        )

    def stop_strategy_runtime(self, runtime_id: str) -> None:
        with self._lock:
            runtime = self._strategy_runtimes.get(runtime_id)
            if runtime is None:
                raise RuntimeError(f"策略运行时不存在: {runtime_id}")
            if runtime.stop_event is not None:
                runtime.stop_event.set()
            runtime.status = "stopping"
            runtime.updated_at = datetime.datetime.now()
            spec = self._runtime_specs.get(runtime_id)
            if spec is not None:
                spec["status"] = "stopped"
                self._save_specs()
        self._data_missing_alarms.discard(f"strategy_data:{runtime_id}")
        try:
            from quantide.service.strategy_tasks import strategy_task_manager

            strategy_task_manager.detach(runtime_id)
        except Exception as exc:
            logger.warning("策略任务卸载失败 {}: {}", runtime_id, exc)

    def start_strategy_runtime(self, runtime_id: str) -> StrategyRuntime:
        with self._lock:
            current = self._strategy_runtimes.get(runtime_id)
            if current is not None and current.status in {"running", "stopping"}:
                return current
            block = self._blocked_strategies.get(runtime_id)
            if block is not None:
                self._record_risk_event(
                    scope="strategy",
                    target_id=runtime_id,
                    title="策略启动被阻止",
                    message=f"策略运行时 {runtime_id} 仍处于风控封锁：{block.get('reason') or '待确认解除'}",
                    severity="critical",
                    blocked=True,
                )
                raise RuntimeError(f"策略运行时已被封锁: {runtime_id}")
            spec = self._runtime_specs.get(runtime_id)
            if spec is None:
                raise RuntimeError(f"策略运行时配置不存在: {runtime_id}")
            account_key = self._account_key(str(spec.get("mode") or ""), str(spec.get("portfolio_id") or ""))
            account_block = self._blocked_accounts.get(account_key)
            if account_block is not None:
                self._record_risk_event(
                    scope="account",
                    target_id=account_key,
                    title="账户启动被阻止",
                    message=(
                        f"账户 {account_key} 仍处于风控封锁，无法恢复策略 {runtime_id}："
                        f"{account_block.get('reason') or '待确认解除'}"
                    ),
                    severity="critical",
                    blocked=True,
                )
                raise RuntimeError(f"账户已被封锁: {account_key}")
            spec["status"] = "running"
            self._save_specs()
        return self._start_from_spec(spec)

    def block_account(self, account_runtime_id: str, reason: str = "手动账户封锁") -> None:
        with self._lock:
            runtime = self._account_runtimes.get(account_runtime_id)
            account_key = runtime.runtime_id if runtime is not None else account_runtime_id
            payload = {
                "target_id": account_key,
                "reason": reason,
                "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            self._blocked_accounts[account_key] = payload
            for item in self._strategy_runtimes.values():
                if self._account_key_for_runtime(item) == account_key:
                    self._apply_runtime_block(item, reason)
                    spec = self._runtime_specs.get(item.runtime_id)
                    if spec is not None:
                        spec["status"] = "blocked"
                        spec["block_scope"] = "account"
                        spec["block_reason"] = reason
            self._save_specs()
            self._record_risk_event(
                scope="account",
                target_id=account_key,
                title="账户已封锁",
                message=f"账户 {account_key} 已被风控封锁：{reason}",
                severity="critical",
                blocked=True,
            )

    def unblock_account(self, account_runtime_id: str) -> None:
        with self._lock:
            block = self._blocked_accounts.pop(account_runtime_id, None)
            if block is None:
                return
            reason = str(block.get("reason") or "手动解除")
            for item in self._strategy_runtimes.values():
                if self._account_key_for_runtime(item) == account_runtime_id and item.runtime_id not in self._blocked_strategies:
                    if item.status == "blocked":
                        item.status = "stopped"
                        item.error = ""
                        item.updated_at = datetime.datetime.now()
            for spec in self._runtime_specs.values():
                if self._account_key(str(spec.get("mode") or ""), str(spec.get("portfolio_id") or "")) != account_runtime_id:
                    continue
                if str(spec.get("block_scope") or "") == "account":
                    spec["status"] = "stopped"
                    spec.pop("block_scope", None)
                    spec.pop("block_reason", None)
            self._save_specs()
            self._record_risk_event(
                scope="account",
                target_id=account_runtime_id,
                title="账户已解除封锁",
                message=f"账户 {account_runtime_id} 已确认解除封锁（原因为：{reason}）。",
                severity="info",
                blocked=False,
            )

    def block_strategy(self, runtime_id: str, reason: str = "手动策略封锁") -> None:
        with self._lock:
            payload = {
                "target_id": runtime_id,
                "reason": reason,
                "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            self._blocked_strategies[runtime_id] = payload
            runtime = self._strategy_runtimes.get(runtime_id)
            if runtime is not None:
                self._apply_runtime_block(runtime, reason)
            spec = self._runtime_specs.get(runtime_id)
            if spec is not None:
                spec["status"] = "blocked"
                spec["block_scope"] = "strategy"
                spec["block_reason"] = reason
            self._save_specs()
            self._record_risk_event(
                scope="strategy",
                target_id=runtime_id,
                title="策略已封锁",
                message=f"策略运行时 {runtime_id} 已被风控封锁：{reason}",
                severity="warning",
                blocked=True,
            )

    def unblock_strategy(self, runtime_id: str) -> None:
        with self._lock:
            block = self._blocked_strategies.pop(runtime_id, None)
            if block is None:
                return
            reason = str(block.get("reason") or "手动解除")
            runtime = self._strategy_runtimes.get(runtime_id)
            if runtime is not None and runtime.status == "blocked":
                runtime.status = "stopped"
                runtime.error = ""
                runtime.updated_at = datetime.datetime.now()
            spec = self._runtime_specs.get(runtime_id)
            if spec is not None:
                spec["status"] = "stopped"
                spec.pop("block_scope", None)
                spec.pop("block_reason", None)
            self._save_specs()
            self._record_risk_event(
                scope="strategy",
                target_id=runtime_id,
                title="策略已解除封锁",
                message=f"策略运行时 {runtime_id} 已确认解除封锁（原因为：{reason}）。",
                severity="info",
                blocked=False,
            )

    def record_system_event(
        self,
        *,
        title: str,
        message: str,
        severity: str = "warning",
        blocked: bool = True,
        target_id: str = "system",
    ) -> None:
        """记录系统级事件（数据就绪、行情中断等）。

        ``blocked=True`` 会进入 header 告警计数；恢复事件使用 ``blocked=False``，
        并同时把同一 ``target_id`` 的未恢复告警标记为已解决。
        """
        if not blocked and target_id:
            with self._lock:
                resolved_at = datetime.datetime.now().isoformat(timespec="seconds")
                for event in self._risk_events:
                    if (
                        str(event.get("target_id") or "") == target_id
                        and event.get("blocked")
                    ):
                        event["blocked"] = False
                        event["resolved_at"] = resolved_at
        self._record_risk_event(
            scope="system",
            target_id=target_id,
            title=title,
            message=message,
            severity=severity,
            blocked=blocked,
        )

    def list_risk_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._risk_events[:limit]]

    def risk_summary(self) -> dict[str, int]:
        with self._lock:
            return {
                "blocked_accounts": len(self._blocked_accounts),
                "blocked_strategies": len(self._blocked_strategies),
                "open_events": sum(1 for item in self._risk_events if item.get("blocked")),
                "event_count": len(self._risk_events),
            }

    def runtime_summary(self) -> dict[str, int]:
        """汇总当前运行时状态。"""
        summary = {
            "total": 0,
            "running": 0,
            "blocked": 0,
            "failed": 0,
            "idle": 0,
        }
        for row in self.list_runtime_rows():
            summary["total"] += 1
            status = str(row.get("status") or "idle")
            if status in summary:
                summary[status] += 1
            else:
                summary["idle"] += 1
        return summary

    def list_runtime_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._lock:
            for item in self._account_runtimes.values():
                rows.append(self._runtime_to_row(item))
            for item in self._backtest_runtimes.values():
                rows.append(
                    {
                        "runtime_id": item.runtime_id,
                        "mode": "backtest",
                        "portfolio_id": item.portfolio_id,
                        "strategy_name": item.strategy_name,
                        "strategy_id": "",
                        "status": item.status,
                        "principal": item.initial_cash,
                        "cash": 0.0,
                        "market_value": 0.0,
                        "total": 0.0,
                        "positions": 0,
                        "orders": 0,
                        "updated_at": item.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "error": item.error,
                    }
                )
            for item in self._strategy_runtimes.values():
                rows.append(self._runtime_to_row(item))
        rows.sort(key=lambda x: (x["mode"], x["portfolio_id"], x["strategy_id"]))
        return rows

    def get_runtime(self, runtime_id: str) -> StrategyRuntime | None:
        """按 runtime_id 获取运行时实例。"""
        with self._lock:
            return self._strategy_runtimes.get(runtime_id)

    def backtest_deployment_modes(self, portfolio_id: str) -> dict[str, dict[str, Any]]:
        """返回某个回测当前活跃的投放模式。

        Args:
            portfolio_id: 回测组合 ID。

        Returns:
            以模式为键的运行时摘要，仅包含当前仍处于活跃状态的 paper/live 运行时。
        """
        active_statuses = {"running", "stopping", "blocked"}
        result: dict[str, dict[str, Any]] = {}
        for row in self.list_runtime_rows():
            if str(row.get("source_backtest_portfolio_id") or "") != portfolio_id:
                continue
            mode = str(row.get("mode") or "")
            if mode not in {"paper", "live"}:
                continue
            if str(row.get("status") or "") not in active_statuses:
                continue
            result[mode] = row
        return result

    def get_active_backtest_deployment(
        self,
        portfolio_id: str,
        mode: str,
    ) -> StrategyRuntime | None:
        """获取某个回测在指定模式下的活跃运行时。"""
        active_statuses = {"running", "stopping", "blocked"}
        with self._lock:
            for runtime in self._strategy_runtimes.values():
                if runtime.mode != mode:
                    continue
                if runtime.source_backtest_portfolio_id != portfolio_id:
                    continue
                if runtime.status not in active_statuses:
                    continue
                return runtime
        return None

    def _runtime_to_row(self, runtime: StrategyRuntime) -> dict[str, Any]:
        cash = 0.0
        mv = 0.0
        total = 0.0
        positions = 0
        orders = 0
        try:
            asset = db.get_asset(portfolio_id=runtime.portfolio_id)
            if asset is not None:
                cash = float(asset.cash)
                mv = float(asset.market_value)
                total = float(asset.total)
        except Exception:
            pass
        try:
            pos_df = db.get_positions(portfolio_id=runtime.portfolio_id)
            positions = pos_df.height
        except Exception:
            positions = 0
        try:
            ord_df = db.get_orders(portfolio_id=runtime.portfolio_id)
            orders = ord_df.height
        except Exception:
            orders = 0
        account_key = self._account_key_for_runtime(runtime)
        account_block = self._blocked_accounts.get(account_key)
        strategy_block = self._blocked_strategies.get(runtime.runtime_id) if runtime.strategy_id else None
        blocked_scope = ""
        block_reason = ""
        if account_block is not None:
            blocked_scope = "account"
            block_reason = str(account_block.get("reason") or "")
        elif strategy_block is not None:
            blocked_scope = "strategy"
            block_reason = str(strategy_block.get("reason") or "")
        status = "blocked" if blocked_scope else runtime.status
        return {
            "runtime_id": runtime.runtime_id,
            "mode": runtime.mode,
            "portfolio_id": runtime.portfolio_id,
            "source_backtest_portfolio_id": runtime.source_backtest_portfolio_id,
            "strategy_name": runtime.strategy_name,
            "strategy_id": runtime.strategy_id,
            "status": status,
            "principal": runtime.principal,
            "cash": cash,
            "market_value": mv,
            "total": total,
            "positions": positions,
            "orders": orders,
            "updated_at": runtime.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "error": runtime.error,
            "alert_text": block_reason or runtime.error,
            "blocked_scope": blocked_scope,
            "block_target": account_key if blocked_scope == "account" else runtime.runtime_id,
            "can_stop": bool(runtime.strategy_id) and runtime.status in {"running", "stopping"} and not blocked_scope,
            "can_start": bool(runtime.strategy_id) and runtime.status in {"stopped", "failed"} and not blocked_scope,
            "can_block_account": not runtime.strategy_id and not blocked_scope,
            "can_unblock_account": not runtime.strategy_id and blocked_scope == "account",
            "can_block_strategy": bool(runtime.strategy_id) and not blocked_scope,
            "can_unblock_strategy": bool(runtime.strategy_id) and blocked_scope == "strategy",
        }

    def _resolve_backtest_run(self, portfolio_id: str) -> BacktestRun:
        run = self.get_backtest_run(portfolio_id)
        if run is not None:
            return run
        portfolio = db.get_portfolio(portfolio_id)
        if portfolio is None:
            raise RuntimeError(f"回测记录不存在: {portfolio_id}")
        strategy_name = portfolio.name or ""
        if not strategy_name:
            raise RuntimeError("无法识别回测策略名")
        runtime_id = f"backtest:{portfolio_id}"
        spec = self._runtime_specs.get(runtime_id)
        if spec is not None:
            config = dict(spec.get("config") or {})
            cheat = bool(spec.get("cheat_on_close", False))
            cheat_tm = str(spec.get("cheat_on_close_time") or "")
        else:
            strategies = strategy_loader.load_from_cache()
            strategy_cls = strategies.get(strategy_name)
            config = dict(getattr(strategy_cls, "PARAMS", {})) if strategy_cls else {}
            cheat = bool(config.get("cheat_on_close", False))
            cheat_tm = get_cheat_on_close_time() if cheat else ""
        if spec is not None:
            if "interval" not in spec or not str(spec.get("interval") or "").strip():
                raise RuntimeError("无法验证持久化策略周期：记录缺少 interval")
            resolved_interval = str(spec["interval"]).strip().lower()
        elif strategy_cls is not None:
            try:
                from quantide.service.runner import BacktestRunner

                resolved_interval = BacktestRunner.strategy_interval(strategy_cls)
            except Exception as exc:
                raise RuntimeError(
                    f"无法验证策略 {strategy_name!r} 的周期声明"
                ) from exc
        else:
            raise RuntimeError(f"无法验证策略 {strategy_name!r} 的周期声明")
        if resolved_interval not in {"1d", "30m"}:
            raise RuntimeError(
                f"无法验证持久化策略周期: {resolved_interval!r}"
            )
        return BacktestRun(
            runtime_id=runtime_id,
            portfolio_id=portfolio_id,
            strategy_name=strategy_name,
            config=config,
            interval=resolved_interval,
            start_date=str(portfolio.start),
            end_date=str(portfolio.end),
            initial_cash=0,
            status=str(spec.get("status") or "finished") if spec is not None else "finished",
            error=str(spec.get("error") or "") if spec is not None else "",
            save_logs=get_backtest_log_path(portfolio_id).exists(),
            log_path=str(get_backtest_log_path(portfolio_id)),
            cheat_on_close=cheat,
            cheat_on_close_time=cheat_tm,
        )

    @staticmethod
    def _validate_config_interval(config: dict[str, Any] | None, interval: str) -> None:
        configured = (config or {}).get("bar_interval")
        if configured is not None and str(configured).strip().lower() != interval:
            raise ValueError(
                f"bar_interval override {configured!r} conflicts with persisted strategy interval {interval!r}"
            )

    def validate_runtime_interval(
        self, spec: dict[str, Any], strategy_cls: Any | None = None
    ) -> str:
        persisted = str(spec.get("interval") or "").strip().lower()
        if persisted not in {"1d", "30m"}:
            raise ValueError(f"无法验证持久化策略周期: {persisted!r}")
        self._validate_config_interval(spec.get("config") or {}, persisted)
        if strategy_cls is not None:
            from quantide.service.runner import BacktestRunner

            declared = BacktestRunner.strategy_interval(strategy_cls)
            if declared != persisted:
                raise ValueError(
                    f"persisted interval {persisted!r} conflicts with strategy declaration {declared!r}"
                )
        return persisted

    def _start_strategy_runtime(
        self,
        mode: str,
        strategy_name: str,
        config: dict[str, Any],
        broker: Any,
        portfolio_id: str,
        source_backtest_portfolio_id: str,
        account_kind: str,
        interval: str,
        market_data: Any,
        principal: float = 0.0,
        runtime_id: str | None = None,
        strategy_id: str | None = None,
        persist: bool = True,
    ) -> StrategyRuntime:
        if mode == "live":
            require_live_trading()
        strategy_id = strategy_id or f"{strategy_name}-{uuid.uuid4().hex[:8]}"
        runtime_id = runtime_id or f"{mode}:{portfolio_id}:{strategy_id}"
        strategy_cls = strategy_loader.load_from_cache().get(strategy_name)
        if strategy_cls is not None:
            from quantide.service.runner import BacktestRunner

            BacktestRunner.validate_strategy_interval(strategy_cls, config, interval)
        account_key = self._account_key(mode, portfolio_id)
        account_block = self._blocked_accounts.get(account_key)
        if account_block is not None:
            self._record_risk_event(
                scope="account",
                target_id=account_key,
                title="运行时启动被阻止",
                message=f"账户 {account_key} 已被封锁，阻止启动运行时 {runtime_id}。",
                severity="critical",
                blocked=True,
            )
            raise RuntimeError(f"账户已被封锁: {account_key}")
        strategy_block = self._blocked_strategies.get(runtime_id)
        if strategy_block is not None:
            self._record_risk_event(
                scope="strategy",
                target_id=runtime_id,
                title="运行时启动被阻止",
                message=f"策略运行时 {runtime_id} 已被封锁，无法启动。",
                severity="critical",
                blocked=True,
            )
            raise RuntimeError(f"策略运行时已被封锁: {runtime_id}")
        symbols = self._extract_symbols(config)
        stop_event = threading.Event()
        runtime = StrategyRuntime(
            runtime_id=runtime_id,
            mode=mode,
            strategy_name=strategy_name,
            strategy_id=strategy_id,
            portfolio_id=portfolio_id,
            source_backtest_portfolio_id=source_backtest_portfolio_id,
            account_kind=account_kind,
            status="running",
            config=config,
            interval=interval,
            symbols=symbols,
            principal=principal,
            stop_event=stop_event,
            broker=broker,
        )
        runtime.thread = threading.Thread(
            target=self._run_strategy_loop,
            args=(runtime, interval, market_data),
            daemon=True,
            name=f"strategy-{strategy_id}",
        )
        with self._lock:
            self._strategy_runtimes[runtime_id] = runtime
            if persist:
                self._runtime_specs[runtime_id] = {
                    "runtime_id": runtime_id,
                    "mode": mode,
                    "strategy_name": strategy_name,
                    "strategy_id": strategy_id,
                    "portfolio_id": portfolio_id,
                    "source_backtest_portfolio_id": source_backtest_portfolio_id,
                    "account_kind": account_kind,
                    "status": "running",
                    "config": config,
                    "symbols": symbols,
                    "principal": principal,
                    "interval": interval,
                }
                self._save_specs()
        try:
            from quantide.service.strategy_tasks import strategy_task_manager

            strategy_task_manager.attach(runtime)
        except Exception as exc:
            logger.warning("策略任务挂载失败 {}: {}", runtime_id, exc)
        runtime.thread.start()
        return runtime

    def _run_strategy_loop(self, runtime: StrategyRuntime, interval: str, market_data: Any) -> None:
        asyncio.run(self._strategy_loop(runtime, interval, market_data))

    def _apply_live_broker_config(self, runtime: StrategyRuntime) -> None:
        """把 strategy config / 类属性中的 cheat_on_close / execution_window / slippage 注入 broker wrapper.

        #45 followup: 原 set_strategy_runtime_config 从未被调用,
        GatewayBrokerWrapper._strategy_cheat_on_close 永远默认 True,
        导致 cheat_on_close=False 路径上的 DeferredOrderQueue 实际是死代码.
        #45 followup2: config 缺 cheat_on_close 时, 读策略类属性 (与 BacktestRunner._resolve_cheat_on_close 对齐),
        否则把 cheat_on_close=True 类属性策略强制改成 deferred-order 路径, 违反 #46 契约.
        """
        config = runtime.config or {}
        if "cheat_on_close" in config:
            cheat_on_close = bool(config["cheat_on_close"])
        else:
            try:
                strategy_cls = strategy_loader.load_from_cache().get(runtime.strategy_name)
            except Exception:
                strategy_cls = None
            cheat_on_close = bool(getattr(strategy_cls, "cheat_on_close", False))
        if str(config.get("fill_timing", "")).strip().lower() in {
            "next_open",
            "open",
        }:
            # The runtime-owned durable queue already controls the delay.  The
            # gateway must submit when the queue is drained rather than put
            # the same order into its legacy in-memory deferred queue again.
            cheat_on_close = True
        live_execution_window = str(config.get("live_execution_window", "auction"))
        live_execution_slippage = float(config.get("live_execution_slippage", 0.001))
        setter = getattr(runtime.broker, "set_strategy_runtime_config", None)
        if not callable(setter):
            return
        setter(
            cheat_on_close=cheat_on_close,
            live_execution_window=live_execution_window,
            live_execution_slippage=live_execution_slippage,
        )

    async def _strategy_loop(self, runtime: StrategyRuntime, interval: str, market_data: Any) -> None:
        strategies = strategy_loader.load_from_cache()
        strategy_cls = strategies.get(runtime.strategy_name)
        if strategy_cls is None:
            self._mark_runtime_failed(
                runtime, RuntimeError(f"策略不存在: {runtime.strategy_name}")
            )
            return
        try:
            if interval in {"1d", "30m"} or isinstance(strategy_cls, type):
                self.validate_runtime_interval(
                    {"interval": interval, "config": runtime.config}, strategy_cls
                )
            if runtime.mode == "live":
                self._apply_live_broker_config(runtime)
        except Exception as exc:
            self._mark_runtime_failed(runtime, exc)
            logger.exception("strategy runtime failed during preflight: {}", exc)
            return
        broker = runtime.broker
        if runtime.mode == "live":
            broker = StrategyBrokerProxy(runtime.broker, runtime.strategy_id)
        strategy: Any = None
        risk_strategies: list[Any] = []
        try:
            strategy = strategy_cls(broker, runtime.config)
            frame = FrameType.MIN30 if interval == "30m" else FrameType.DAY
            risk_strategies = self._build_live_risk_strategies(
                strategy_cls, broker, runtime.config, frame_type=frame
            )
        except Exception as exc:
            self._mark_runtime_failed(runtime, exc)
            logger.exception("strategy runtime failed during initialization: {}", exc)
            return
        fill_timing_value = (runtime.config or {}).get(
            "fill_timing", getattr(strategy_cls, "fill_timing", None)
        )
        if fill_timing_value is None:
            defaults = getattr(strategy_cls, "default_config", None)
            if callable(defaults):
                fill_timing_value = defaults().get("fill_timing")
        fill_timing = str(fill_timing_value or "").strip().lower()
        daily_next_open = interval == "1d" and fill_timing in {"next_open", "open"}
        strategy.interval = frame.value
        risk_day: datetime.date | None = None
        last_daily_decision: datetime.date | None = None
        last_30m_bar: datetime.datetime | None = None
        next_open_30m = interval == "30m" and fill_timing in {"next_open", "open"}
        with self._lock:
            persisted_decision = str(
                (self._runtime_specs.get(runtime.runtime_id) or {}).get(
                    "last_decision_date", ""
                )
            )
        if persisted_decision:
            try:
                last_daily_decision = datetime.date.fromisoformat(persisted_decision)
            except ValueError:
                last_daily_decision = None
        try:
            await strategy.init()
            await strategy.on_start()
            for risk_strategy in risk_strategies:
                await risk_strategy.init()
                await risk_strategy.on_start()
            while runtime.stop_event is not None and not runtime.stop_event.is_set():
                now = datetime.datetime.now()
                if hasattr(runtime.broker, "set_clock"):
                    runtime.broker.set_clock(now)
                # SC-04: prime broker cache with the latest snapshot before the
                # callback. ``on_bar`` is the canonical single-argument form;
                # the strategy pulls its data through ``get_bars``/``get_history``.
                self._build_quotes(runtime.symbols, market_data)
                strategy._current_time = now
                if risk_strategies and risk_day != now.date():
                    if risk_day is not None:
                        for risk_strategy in risk_strategies:
                            await risk_strategy.on_day_close(now)
                    for risk_strategy in risk_strategies:
                        await risk_strategy.on_day_open(now)
                    risk_day = now.date()
                # Risk programs must get the first chance to reduce an open
                # position.  The host may deliberately repurchase on the same
                # bar when allow_same_bar_repurchase is true.
                for risk_strategy in risk_strategies:
                    await risk_strategy.on_check(broker.positions, now)
                if interval == "30m":
                    completed_bar = self._latest_completed_30m_bar(now)
                    if next_open_30m:
                        # Drain persisted orders before replaying the current
                        # bar.  A restart therefore submits an old target bar
                        # first, and the replayed signal is deduplicated by its
                        # deterministic (runtime, signal bar, order) key.
                        await self.process_next_open_30m_intents(
                            runtime.runtime_id, now
                        )
                    dispatch_bar = (
                        completed_bar is not None
                        and completed_bar != last_30m_bar
                    )
                    if dispatch_bar:
                        if next_open_30m:
                            next_open_bar_broker = NextOpenBarBrokerProxy(
                                broker,
                                runtime_id=runtime.runtime_id,
                                portfolio_id=runtime.portfolio_id,
                                strategy_id=runtime.strategy_id,
                                signal_bar=completed_bar,
                            )
                            original_broker = strategy.broker
                            strategy.broker = next_open_bar_broker
                        else:
                            original_broker = None
                        try:
                            strategy._current_time = completed_bar
                            await strategy.on_bar(completed_bar)
                        finally:
                            if next_open_30m:
                                strategy.broker = original_broker
                        last_30m_bar = completed_bar
                elif daily_next_open:
                    decision_day = self._next_open_decision_day(now)
                    if decision_day is not None and decision_day != last_daily_decision:
                        original_broker = strategy.broker
                        strategy.broker = NextOpenBrokerProxy(
                            broker,
                            runtime_id=runtime.runtime_id,
                            portfolio_id=runtime.portfolio_id,
                            strategy_id=runtime.strategy_id,
                            signal_date=decision_day,
                        )
                        try:
                            await strategy.on_bar(now)
                        finally:
                            strategy.broker = original_broker
                        last_daily_decision = decision_day
                        with self._lock:
                            spec = self._runtime_specs.get(runtime.runtime_id)
                            if spec is not None:
                                spec["last_decision_date"] = decision_day.isoformat()
                                self._save_specs()
                else:
                    # Existing continuous callback behavior for non-queued
                    # strategies is intentionally unchanged.
                    await strategy.on_bar(now)
                window_check = getattr(strategy, "_in_decision_window", None)
                if window_check is None or window_check(now):
                    self._sync_strategy_data_alarm(
                        runtime, bool(getattr(strategy, "_data_missing", False))
                    )
                runtime.updated_at = datetime.datetime.now()
                await asyncio.sleep(2)
            if runtime.status in {"running", "stopping"}:
                runtime.status = "stopped"
            try:
                from quantide.service.strategy_tasks import strategy_task_manager

                strategy_task_manager.detach(runtime.runtime_id)
            except Exception:
                pass
        except Exception as exc:
            self._mark_runtime_failed(runtime, exc)
            logger.exception("strategy runtime failed: {}", exc)
        finally:
            runtime.updated_at = datetime.datetime.now()
            try:
                from quantide.service.strategy_tasks import strategy_task_manager

                strategy_task_manager.detach(runtime.runtime_id)
            except Exception:
                pass
            with self._lock:
                spec = self._runtime_specs.get(runtime.runtime_id)
                if spec is not None:
                    spec["status"] = runtime.status
                    self._save_specs()
            for risk_strategy in risk_strategies:
                try:
                    await risk_strategy.on_stop()
                except Exception:
                    pass
            if strategy is not None:
                try:
                    await strategy.on_stop()
                except Exception:
                    pass

    def _mark_runtime_failed(self, runtime: StrategyRuntime, exc: Exception) -> None:
        """Persist a runtime failure and expose it through the risk event path."""
        runtime.status = "failed"
        runtime.error = str(exc)
        runtime.updated_at = datetime.datetime.now()
        with self._lock:
            spec = self._runtime_specs.get(runtime.runtime_id)
            if spec is not None:
                spec["status"] = "failed"
                spec["error"] = runtime.error
                self._save_specs()
        missing_dependency = "requires the lightgbm package" in runtime.error
        self.record_system_event(
            title="策略运行失败",
            message=f"策略运行时 {runtime.runtime_id} 失败：{runtime.error}",
            severity="critical",
            blocked=missing_dependency,
            target_id=runtime.runtime_id,
        )

    def _next_open_decision_day(self, now: datetime.datetime) -> datetime.date | None:
        """Find today's persisted, ready daily signal day.

        A restart before the next open may process an already-persisted intent,
        but it must not recreate yesterday's signal after the next open.  The
        candidate therefore has to be today's bar and pass the post-close
        persistence gate.
        """
        from quantide.service.data_readiness import data_readiness_monitor

        try:
            candidate = daily_bars.end
        except Exception:
            candidate = None
        if not isinstance(candidate, datetime.date) or candidate != now.date():
            return None
        if not data_readiness_monitor.is_daily_ready(now, day=candidate):
            return None
        return candidate

    @staticmethod
    def _latest_completed_30m_bar(now: datetime.datetime) -> datetime.datetime | None:
        """Return the latest session bar boundary that is fully closed.

        Calendar ticks encode the split morning/afternoon sessions, so flooring
        through this API never manufactures a lunch or overnight bar.  Before
        10:00 there is no completed 30m bar for today's session.
        """
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        tm = now.time()
        if tm < datetime.time(10, 0):
            return None
        try:
            if not calendar.is_trade_day(now):
                return None
            candidate = calendar.floor(now, FrameType.MIN30)
        except Exception:
            return None
        if not isinstance(candidate, datetime.datetime):
            return None
        candidate = candidate.replace(tzinfo=None)
        if candidate.date() != now.date():
            return None
        return candidate if candidate <= now else None

    @staticmethod
    def _next_eligible_30m_bar(signal_bar: datetime.datetime) -> datetime.datetime:
        """Return the next real 30m boundary, crossing session gaps safely."""
        try:
            next_completed = calendar.shift(signal_bar, 1, FrameType.MIN30)
        except Exception as exc:
            raise RuntimeError("无法确定 30m next_open 的目标 bar") from exc
        if not isinstance(next_completed, datetime.datetime):
            raise RuntimeError("30m next_open 的目标 bar 无效")
        return (next_completed - datetime.timedelta(minutes=30)).replace(tzinfo=None)

    async def process_next_open_30m_intents(
        self, runtime_id: str, completed_bar: datetime.datetime
    ) -> int:
        """Submit durable 30m intents whose target bar has completed.

        The row remains pending until the broker call returns.  A processed
        row is never selected again, including after a new manager/store is
        constructed for a restarted process.
        """
        runtime = self.get_runtime(runtime_id)
        if runtime is None or runtime.status not in {"running", "stopping"}:
            return 0
        if str(getattr(runtime, "interval", "")) != "30m":
            return 0
        boundary = completed_bar.replace(tzinfo=None)
        in_flight = tracking_store.list_next_open_intents(
            runtime_id=runtime_id, status="processing"
        )
        if in_flight:
            raise RuntimeError(
                "30m next_open 存在崩溃后未确认的 processing intent，拒绝重提交"
            )
        try:
            if not calendar.is_trade_day(boundary):
                return 0
        except Exception:
            # Test doubles and a not-yet-loaded calendar are handled by the
            # same fail-closed target calculation in the enqueue path.
            pass
        tm = boundary.time()
        if (
            tm < datetime.time(9, 30)
            or datetime.time(11, 30) < tm < datetime.time(13, 0)
            or tm > datetime.time(15, 0)
        ):
            return 0
        broker = runtime.broker
        if runtime.mode == "live":
            broker = StrategyBrokerProxy(runtime.broker, runtime.strategy_id)
        submitted = 0
        rows = tracking_store.list_next_open_intents(
            runtime_id=runtime_id, target_bar=boundary
        )
        for row in rows:
            claimed = tracking_store.claim_next_open_intent(row["intent_id"])
            if claimed is None:
                continue
            row = claimed
            try:
                kwargs = dict(row.get("kwargs") or {})
                target_time = datetime.datetime.fromisoformat(str(row["target_bar"]))
                # Preserve the target boundary for an on-time drain, while a
                # restarted process submits with the actual late arrival time.
                kwargs["order_time"] = max(target_time, boundary)
                method = getattr(broker, str(row["method"]))
                await method(*(row.get("args") or []), **kwargs)
                tracking_store.mark_next_open_intent(
                    row["intent_id"], status="processed"
                )
                submitted += 1
            except Exception as exc:
                tracking_store.mark_next_open_intent(
                    row["intent_id"], status="failed", error=str(exc)
                )
                logger.warning("30m next_open intent 执行失败 {}: {}", row["intent_id"], exc)
        return submitted

    async def process_next_open_intents(
        self, runtime_id: str, now: datetime.datetime | None = None
    ) -> int:
        """Submit due durable intents once at the next trading day's open."""
        runtime = self.get_runtime(runtime_id)
        current = now or datetime.datetime.now()
        if runtime is None or runtime.status not in {"running", "stopping"}:
            return 0
        if current.time() < datetime.time(9, 25):
            return 0
        in_flight = tracking_store.list_next_open_intents(
            runtime_id=runtime_id, status="processing"
        )
        if in_flight:
            raise RuntimeError(
                "next_open 存在崩溃后未确认的 processing intent，拒绝重提交"
            )
        # The persisted target date is the source of truth here.  A freshly
        # installed process may not have the calendar row for that date yet;
        # rejecting the durable intent in that case would strand an order.
        broker = runtime.broker
        if runtime.mode == "live":
            broker = StrategyBrokerProxy(runtime.broker, runtime.strategy_id)
        submitted = 0
        rows = tracking_store.list_next_open_intents(
            runtime_id=runtime_id, target_date=current.date()
        )
        for row in rows:
            try:
                kwargs = dict(row.get("kwargs") or {})
                kwargs["order_time"] = current
                method = getattr(broker, str(row["method"]))
                await method(*(row.get("args") or []), **kwargs)
                tracking_store.mark_next_open_intent(
                    row["intent_id"], status="processed"
                )
                submitted += 1
            except Exception as exc:
                tracking_store.mark_next_open_intent(
                    row["intent_id"], status="failed", error=str(exc)
                )
                logger.warning("next_open intent 执行失败 {}: {}", row["intent_id"], exc)
        return submitted

    def _build_live_risk_strategy(
        self, strategy_cls: Any, broker: Any, config: dict[str, Any]
    ) -> Any:
        """Backward-compatible singular wrapper for callers outside the loop."""
        strategies = self._build_live_risk_strategies(strategy_cls, broker, config)
        return strategies[0] if strategies else None

    def _build_live_risk_strategies(
        self,
        strategy_cls: Any,
        broker: Any,
        config: dict[str, Any],
        frame_type: FrameType | str = FrameType.DAY,
    ) -> list[Any]:
        """Build only explicitly adapted selected risk programs."""
        return build_live_risk_strategies(
            strategy_cls, broker, config, frame_type=frame_type
        )

    def _build_quotes(self, symbols: list[str], market_data: Any) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not symbols:
            symbols = ["000001.SZ"]
        if market_data is None:
            return result
        snaps = market_data.snapshot(symbols)
        for symbol in symbols:
            snap = snaps.get(symbol)
            if snap is None:
                continue
            result[symbol] = {
                "lastPrice": float(snap.price or 0),
                "open": float(snap.open or 0),
                "high": float(snap.high or 0),
                "low": float(snap.low or 0),
                "volume": float(snap.volume or 0),
                "amount": float(snap.amount or 0),
            }
        return result

    def _extract_symbols(self, config: dict[str, Any]) -> list[str]:
        for key in ("symbol", "asset", "security"):
            value = config.get(key)
            if isinstance(value, str) and value:
                return [value]
        for key in ("symbols", "assets", "securities"):
            value = config.get(key)
            if isinstance(value, list):
                symbols = [str(item) for item in value if item]
                if symbols:
                    return symbols
        return []

    def _state_file(self) -> Path:
        return get_strategy_runtime_state_path()

    def _save_specs(self) -> None:
        file_path = self._state_file()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy_runtimes": list(self._runtime_specs.values()),
            "blocked_accounts": list(self._blocked_accounts.values()),
            "blocked_strategies": list(self._blocked_strategies.values()),
            "risk_events": self._risk_events[:100],
        }
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_specs(self) -> None:
        file_path = self._state_file()
        if not file_path.exists():
            return
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            items = payload.get("strategy_runtimes") or []
            mapping: dict[str, dict[str, Any]] = {}
            for item in items:
                runtime_id = str(item.get("runtime_id") or "")
                if runtime_id:
                    mapping[runtime_id] = item
            self._runtime_specs = mapping
            self._blocked_accounts = {
                str(item.get("target_id") or ""): item
                for item in payload.get("blocked_accounts") or []
                if str(item.get("target_id") or "")
            }
            self._blocked_strategies = {
                str(item.get("target_id") or ""): item
                for item in payload.get("blocked_strategies") or []
                if str(item.get("target_id") or "")
            }
            self._risk_events = [
                item for item in (payload.get("risk_events") or []) if isinstance(item, dict)
            ]
        except Exception:
            self._runtime_specs = {}
            self._blocked_accounts = {}
            self._blocked_strategies = {}
            self._risk_events = []

    def _restore_persisted_runtimes(self) -> None:
        specs = list(self._runtime_specs.values())
        for spec in specs:
            if str(spec.get("status") or "").lower() != "running":
                continue
            runtime_id = str(spec.get("runtime_id") or "")
            if str(spec.get("mode") or "").lower() == "live" and not supports_live_trading():
                reason = "CE edition 暂不支持实盘交易，未恢复该实盘运行时"
                spec["status"] = "blocked"
                spec["block_scope"] = "edition"
                spec["block_reason"] = reason
                spec["error"] = reason
                self._save_specs()
                self._record_risk_event(
                    scope="system",
                    target_id=runtime_id,
                    title="重启恢复阻止：实盘功能不可用",
                    message=reason,
                    severity="critical",
                    blocked=True,
                )
                continue
            from quantide.service.risk_programs import unavailable_risk_program_ids

            unavailable = unavailable_risk_program_ids(
                (spec.get("config") or {}).get("risk_programs") or [],
                frame_type=str(spec.get("interval") or "1d"),
            )
            if unavailable:
                reason = "风险程序不可用/已阻止：" + ", ".join(unavailable)
                spec["status"] = "blocked"
                spec["block_scope"] = "strategy"
                spec["block_reason"] = reason
                spec["error"] = reason
                self._save_specs()
                self._record_risk_event(
                    scope="strategy",
                    target_id=runtime_id,
                    title="重启恢复阻止：风险程序不可用",
                    message=f"运行时 {runtime_id} 未恢复：{reason}",
                    severity="critical",
                    blocked=True,
                )
                continue
            account_key = self._account_key(str(spec.get("mode") or ""), str(spec.get("portfolio_id") or ""))
            account_block = self._blocked_accounts.get(account_key)
            strategy_block = self._blocked_strategies.get(runtime_id)
            if account_block is not None or strategy_block is not None:
                reason = ""
                scope = "account"
                if account_block is not None:
                    reason = str(account_block.get("reason") or "")
                elif strategy_block is not None:
                    scope = "strategy"
                    reason = str(strategy_block.get("reason") or "")
                spec["status"] = "blocked"
                spec["block_scope"] = scope
                spec["block_reason"] = reason
                self._save_specs()
                self._record_risk_event(
                    scope=scope,
                    target_id=account_key if scope == "account" else runtime_id,
                    title="重启恢复跳过封锁运行时",
                    message=(
                        f"重启恢复时跳过运行时 {runtime_id}，因为{scope}仍被封锁：{reason or '待确认解除'}"
                    ),
                    severity="warning",
                    blocked=True,
                )
                continue
            try:
                self._start_from_spec(spec)
                self._record_risk_event(
                    scope="system",
                    target_id=runtime_id,
                    title="重启恢复完成",
                    message=f"重启恢复已重新启动运行时 {runtime_id}。",
                    severity="info",
                    blocked=False,
                )
            except Exception as exc:
                spec["status"] = "failed"
                spec["error"] = str(exc)
                self._save_specs()
                self._record_risk_event(
                    scope="system",
                    target_id=runtime_id,
                    title="重启恢复失败",
                    message=f"运行时 {runtime_id} 在重启恢复时失败：{exc}",
                    severity="critical",
                    blocked=False,
                )

    def _account_key(self, mode: str, portfolio_id: str) -> str:
        return f"{mode}:{portfolio_id}"

    def _account_key_for_runtime(self, runtime: StrategyRuntime) -> str:
        return self._account_key(runtime.mode, runtime.portfolio_id)

    def _apply_runtime_block(self, runtime: StrategyRuntime, reason: str) -> None:
        if runtime.stop_event is not None:
            runtime.stop_event.set()
        runtime.status = "blocked"
        runtime.error = reason
        runtime.updated_at = datetime.datetime.now()
        getattr(self, "_data_missing_alarms", set()).discard(
            f"strategy_data:{runtime.runtime_id}"
        )
        try:
            from quantide.service.strategy_tasks import strategy_task_manager

            strategy_task_manager.detach(runtime.runtime_id)
        except Exception:
            pass

    def _sync_strategy_data_alarm(
        self, runtime: StrategyRuntime, missing: bool
    ) -> None:
        """策略行情缺失/恢复时写系统风险事件（避免静默跳过选股）。"""
        target = f"strategy_data:{runtime.runtime_id}"
        if missing and target not in self._data_missing_alarms:
            self._data_missing_alarms.add(target)
            self.record_system_event(
                title="策略行情缺失",
                message=(
                    f"{runtime.strategy_name or runtime.runtime_id} "
                    f"今日行情/覆盖不足，已跳过选股。"
                ),
                severity="critical",
                blocked=True,
                target_id=target,
            )
        elif not missing and target in self._data_missing_alarms:
            self._data_missing_alarms.discard(target)
            self.record_system_event(
                title="策略行情恢复",
                message=f"{runtime.strategy_name or runtime.runtime_id} 行情已恢复。",
                severity="info",
                blocked=False,
                target_id=target,
            )

    def _record_risk_event(
        self,
        *,
        scope: str,
        target_id: str,
        title: str,
        message: str,
        severity: str,
        blocked: bool,
    ) -> None:
        event = {
            "event_id": uuid.uuid4().hex[:8],
            "scope": scope,
            "target_id": target_id,
            "title": title,
            "message": message,
            "severity": severity,
            "blocked": blocked,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self._risk_events.insert(0, event)
        self._risk_events = self._risk_events[:100]
        self._save_specs()

    def _start_from_spec(self, spec: dict[str, Any]) -> StrategyRuntime:
        if str(spec.get("mode") or "").lower() == "live":
            require_live_trading()
        if self._registry is None:
            raise RuntimeError("registry 未初始化")
        account_kind = str(spec.get("account_kind") or "")
        portfolio_id = str(spec.get("portfolio_id") or "")
        broker = None
        if account_kind == "gateway":
            broker = self._gateway_broker
        if broker is None:
            broker = self._registry.get(account_kind, portfolio_id)
        if broker is None:
            raise RuntimeError(f"账户不存在: {account_kind}:{portfolio_id}")
        if "interval" not in spec or not str(spec.get("interval") or "").strip():
            raise RuntimeError("持久化运行时缺少不可变策略周期")
        interval = str(spec["interval"]).strip().lower()
        if account_kind == BrokerKind.SIMULATION.value:
            port = getattr(broker, "port", None)
            apply_fee_config = getattr(port, "apply_fee_config", None)
            if callable(apply_fee_config):
                config = dict(spec.get("config") or {})
                apply_fee_config(
                    commission=_optional_float(config.get("commission")),
                    stamp_tax=_optional_float(config.get("stamp_duty")),
                    transfer_fee=_optional_float(config.get("transfer_fee")),
                    min_commission=_optional_float(config.get("min_commission")),
                    slippage=_optional_float(config.get("slippage_pct")),
                )
        return self._start_strategy_runtime(
            mode=str(spec.get("mode") or ""),
            strategy_name=str(spec.get("strategy_name") or ""),
            config=dict(spec.get("config") or {}),
            broker=broker,
            portfolio_id=portfolio_id,
            source_backtest_portfolio_id=str(spec.get("source_backtest_portfolio_id") or ""),
            account_kind=account_kind,
            interval=interval,
            market_data=self._market_data,
            principal=float(spec.get("principal") or 0),
            runtime_id=str(spec.get("runtime_id") or ""),
            strategy_id=str(spec.get("strategy_id") or ""),
            persist=False,
        )


strategy_runtime_manager = StrategyRuntimeManager()
