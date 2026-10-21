import datetime
import inspect
import json
import uuid
from typing import Any

from loguru import logger

from quantide.config.settings import get_cheat_on_close_time
from quantide.core.enums import FrameType
from quantide.core.errors import RiskStrategyNotBacktestable
from quantide.core.ports import ClockPort
from quantide.core.runtime.clock_bridge import BacktestClockAdapter
from quantide.core.strategy import BaseStrategy, RiskStrategy
from quantide.data.models.calendar import calendar
from quantide.data.models.daily_bars import daily_bars
from quantide.data.sqlite import db
from quantide.service.backtest_broker import BacktestBroker
from quantide.service.backtest_logs import record_backtest_log
from quantide.service.metrics import metrics
from quantide.service.risk_overlay import (
    TripleBarrierConfig,
    TripleBarrierOverlay,
)


class BacktestRunner:
    """回测运行器，负责管理回测的生命周期和时间循环。"""

    def __init__(self, clock: ClockPort | None = None):
        """初始化回测运行器.

        Args:
            clock: 时钟端口实现。
        """
        self._clock = clock or BacktestClockAdapter()

    @staticmethod
    def _resolve_cheat_on_close(
        strategy_cls: type[BaseStrategy], config: dict[str, Any]
    ) -> bool:
        """从 config / 类属性解析 cheat_on_close. config 优先."""
        if "cheat_on_close" in config:
            return bool(config["cheat_on_close"])
        return bool(getattr(strategy_cls, "cheat_on_close", False))

    @staticmethod
    def _resolve_cheat_on_close_time(cheat: bool) -> tuple[int, int]:
        """返回 (hour, minute) 触发时刻. cheat=True 走 settings.cheat_on_close_time, 否则 9:30."""
        if not cheat:
            return (9, 30)
        time_str = get_cheat_on_close_time()
        h, m = map(int, time_str.split(":"))
        return (h, m)

    def _align_backtest_dates(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> tuple[datetime.date, datetime.date]:
        """对齐回测起止日期到交易日。

        Args:
            start_date: 回测开始日期
            end_date: 回测结束日期

        Returns:
            tuple[datetime.date, datetime.date]: 对齐后的起止日期
        """
        start_date = calendar.ceiling(start_date, FrameType.DAY)
        end_date = calendar.floor(end_date, FrameType.DAY)
        if start_date > end_date:
            raise ValueError(f"回测开始日期 {start_date} 不能晚于结束日期 {end_date}")
        return start_date, self._clamp_end_date_to_market_data(end_date, start_date)

    @staticmethod
    def _clamp_end_date_to_market_data(
        end_date: datetime.date,
        start_date: datetime.date,
    ) -> datetime.date:
        """把回测结束日期收敛到行情数据覆盖的最后交易日。

        行情数据滞后时，最后一根 bar 没有行情，卖出/清仓会因为拿不到
        撮合价而失败，因此这里提前收敛，避免回测在收尾阶段报错。

        Args:
            end_date: 对齐后的回测结束日期。
            start_date: 对齐后的回测开始日期。

        Returns:
            datetime.date: 收敛后的结束日期。
        """
        last_data_date = daily_bars.end
        if not isinstance(last_data_date, datetime.date) or end_date <= last_data_date:
            return end_date
        clamped = calendar.floor(last_data_date, FrameType.DAY)
        if clamped < start_date:
            raise ValueError(
                f"行情数据仅覆盖到 {last_data_date}，无法回测 {start_date} 之后的区间"
            )
        logger.warning(
            "回测结束日期 {} 超出行情数据范围，已收敛到 {}（请更新行情数据后重跑）",
            end_date,
            clamped,
        )
        return clamped

    def _init_backtest(
        self,
        strategy_cls: type[BaseStrategy],
        config: dict[str, Any],
        start_date: datetime.date,
        end_date: datetime.date,
        frame_type: FrameType,
        initial_cash: float,
        portfolio_id: str | None,
        db_path: str | None,
        save_logs: bool,
    ) -> tuple[str, BacktestBroker, BaseStrategy]:
        """初始化回测环境，包括 Broker、Strategy 和数据库。

        Args:
            strategy_cls: 策略类
            config: 策略配置
            start_date: 回测开始日期
            end_date: 回测结束日期
            frame_type: 回测周期类型
            initial_cash: 初始资金
            portfolio_id: 组合 ID，如果为 None 则自动生成
            db_path: 数据库路径，如果为 None 则使用默认路径
            save_logs: 是否同步写入回测日志文件。

        Returns:
            tuple: (portfolio_id, broker, strategy)
        """
        if portfolio_id is None:
            portfolio_id = uuid.uuid4().hex

        # Initialize DB if provided (e.g. for multi-process isolation with :memory:)
        if db_path:
            db.init(db_path)

        # 1. Init Broker
        overlay_name = getattr(strategy_cls, "RISK_OVERLAY", None)
        risk_overlay = None
        if overlay_name == "triple_barrier":
            if "use_broker_barriers" in config:
                use_barriers = bool(config["use_broker_barriers"])
            else:
                defaults = getattr(strategy_cls, "default_config", None)
                use_barriers = (
                    bool(defaults().get("use_broker_barriers", False))
                    if callable(defaults)
                    else False
                )
            if use_barriers:
                risk_overlay = TripleBarrierOverlay(
                    TripleBarrierConfig(
                        up_pct=float(
                            config.get("take_profit_pct", config.get("up_pct", 0.15))
                        ),
                        down_pct=float(
                            config.get("stop_loss_pct", config.get("down_pct", 0.06))
                        ),
                        horizon=int(config.get("horizon", 3)),
                        sell_fraction=float(config.get("sell_fraction", 1.0)),
                        slippage=float(config.get("slippage_pct", 0.001)),
                    )
                )
        broker = BacktestBroker(
            bt_start=start_date,
            bt_end=end_date,
            portfolio_id=portfolio_id,
            data_feed=daily_bars,  # type: ignore
            principal=initial_cash,
            commission=float(config.get("commission", 1e-4)),
            stamp_duty=float(config.get("stamp_duty", 5e-4)),
            min_commission=float(config.get("min_commission", 5.0)),
            transfer_fee=float(config.get("transfer_fee", 1e-5)),
            match_level="day" if frame_type == FrameType.DAY else "minute",
            portfolio_name=strategy_cls.__name__,
            desc=json.dumps({"config": dict(config or {})}, ensure_ascii=False),
            save_logs=save_logs,
            risk_overlay=risk_overlay,
        )

        # Use patched logger
        self.logger = logger.bind(runner="BacktestRunner", portfolio_id=portfolio_id)
        self.logger.info(f"Starting backtest for {strategy_cls.__name__} ({portfolio_id})")
        record_backtest_log(
            portfolio_id=portfolio_id,
            level="INFO",
            source="runner",
            message=(
                f"开始回测：策略={strategy_cls.__name__}，区间={start_date}~{end_date}，"
                f"周期={frame_type.value}，初始资金={initial_cash:.2f}"
            ),
            dt=calendar.replace_time(start_date, 9, 0),
            extra={"interval": frame_type.value, "initial_cash": initial_cash},
            save_to_file=save_logs,
        )

        # 2. Init Strategy
        strategy = strategy_cls(broker, config)
        strategy.interval = frame_type.value

        return portfolio_id, broker, strategy

    async def _handle_day_switch(
        self,
        strategy: BaseStrategy,
        broker: BacktestBroker,
        current_date: datetime.date,
        last_trade_day: datetime.date | None,
    ) -> datetime.date:
        """处理日间切换逻辑（收盘和开盘）。

        Args:
            strategy: 策略实例
            broker: Broker 实例
            current_date: 当前交易日
            last_trade_day: 上一个交易日

        Returns:
            datetime.date: 更新后的 last_trade_day (即 current_date)
        """
        if last_trade_day is None or current_date != last_trade_day:
            # Close previous day if exists
            if last_trade_day is not None:
                close_tm = calendar.replace_time(last_trade_day, 15, 30)
                self._clock.set_now(close_tm)
                broker.set_clock(close_tm)
                strategy._current_time = close_tm
                await strategy.on_day_close(close_tm)

            # Open new day
            open_tm = calendar.replace_time(current_date, 9, 30)
            self._clock.set_now(open_tm)
            broker.set_clock(open_tm)
            strategy._current_time = open_tm
            await strategy.on_day_open(open_tm)

            return current_date
        return last_trade_day

    def _resolve_day_signal_date(
        self,
        current_date: datetime.date,
        bar_tm: datetime.datetime,
    ) -> datetime.date:
        """返回日线策略在当前时间点可见的最新完整交易日。

        日线回测的交易信号在开盘时生成，因此此时只能使用上一交易日
        已经完整收盘的数据，避免“看到当天收盘价后又按当天收盘价成交”的前视偏差。

        Args:
            current_date: 当前交易日。
            bar_tm: 当前 bar 的触发时间。

        Returns:
            可用于生成信号的最近完整交易日。
        """
        if bar_tm.time() <= datetime.time(9, 30):
            return calendar.day_shift(current_date, -1)
        return current_date

    def _get_bar_quote(
        self,
        broker: BacktestBroker,
        current_date: datetime.date,
        bar_tm: datetime.datetime,
        config: dict[str, Any],
        frame_type: FrameType,
    ) -> dict[str, Any]:
        """获取当前 Bar 的行情快照。

        Args:
            broker: Broker 实例
            current_date: 当前日期
            bar_tm: 当前 Bar 时间
            config: 策略配置
            frame_type: 当前 Bar 的周期类型

        Returns:
            Dict[str, Any]: 行情快照字典
        """
        quote = {}
        if frame_type == FrameType.DAY:
            universe = config.get("universe", [])
            assets = list(set(list(broker.positions.keys()) + universe))

            if assets:
                quote_date = self._resolve_day_signal_date(current_date, bar_tm)
                df = daily_bars.get_bars_in_range(quote_date, quote_date, assets)
                if not df.is_empty():
                    for row in df.iter_rows(named=True):
                        quote[row["asset"]] = {
                            "lastPrice": row["close"],
                            "volume": row["volume"],
                        }
        else:
            # TODO: Implement minute bar quote fetching
            pass
        return quote

    async def run(
        self,
        strategy_cls: type[BaseStrategy],
        config: dict[str, Any],
        start_date: datetime.date,
        end_date: datetime.date,
        frame_type: FrameType = FrameType.DAY,
        initial_cash: float = 1_000_000,
        portfolio_id: str | None = None,
        db_path: str | None = None,
        save_logs: bool = False,
    ) -> dict[str, Any]:
        """运行回测。

        Args:
            strategy_cls: 策略类
            config: 策略配置
            start_date: 回测开始日期
            end_date: 回测结束日期
            frame_type: 回测周期类型
            initial_cash: 初始资金
            portfolio_id: 组合 ID，如果为 None 则自动生成
            db_path: 数据库路径，如果为 None 则使用默认路径
            save_logs: 是否同时写入回测日志文件

        Returns:
            Dict[str, Any]: 回测结果，包含 metrics 和 portfolio_id
        """
        if inspect.isclass(strategy_cls) and issubclass(strategy_cls, RiskStrategy):
            raise RiskStrategyNotBacktestable(getattr(strategy_cls, "__name__", ""))

        start_date, end_date = self._align_backtest_dates(start_date, end_date)

        portfolio_id, broker, strategy = self._init_backtest(
            strategy_cls,
            config,
            start_date,
            end_date,
            frame_type,
            initial_cash,
            portfolio_id,
            db_path,
            save_logs,
        )

        await strategy.init()
        await strategy.on_start()

        # 3. Time Loop
        if frame_type in [FrameType.MIN1, FrameType.MIN5]:
            start_tm = calendar.first_min_frame(start_date, frame_type)
            end_tm = calendar.last_min_frame(end_date, frame_type)
            frames = self._clock.iter_frames(start_tm, end_tm, frame_type)
        else:
            frames = self._clock.iter_frames(start_date, end_date, frame_type)

        last_trade_day = None

        try:
            cheat = self._resolve_cheat_on_close(strategy_cls, config)
            cheat_tm = self._resolve_cheat_on_close_time(cheat)
            # SC-04: announce the bar interval on the strategy before the loop
            # so consumers can observe the active frame via ``strategy.interval``.
            strategy.interval = frame_type.value
            for tm in frames:
                if isinstance(tm, datetime.datetime):
                    current_date = tm.date()
                    bar_tm = tm
                else:
                    current_date = tm
                    bar_tm = calendar.replace_time(current_date, *cheat_tm)

                last_trade_day = await self._handle_day_switch(
                    strategy, broker, current_date, last_trade_day
                )

                # Bar Logic
                self._clock.set_now(bar_tm)
                broker.set_clock(bar_tm)
                strategy._current_time = bar_tm
                # SC-04: ``on_bar`` only receives the bar timestamp. Strategies
                # pull data themselves via ``get_bars``/``get_history``. We still
                # compute the pre-callback quote so daily backtests can prime
                # the broker's quote cache through the legacy hook, but the
                # callback signature is the single-argument canonical form.
                self._get_bar_quote(
                    broker,
                    current_date,
                    bar_tm,
                    config,
                    frame_type,
                )
                if frame_type == FrameType.DAY:
                    overlay_result = broker.run_risk_overlay(bar_tm)
                    if inspect.isawaitable(overlay_result):
                        await overlay_result
                await strategy.on_bar(bar_tm)
                if frame_type == FrameType.DAY:
                    broker.sync_risk_overlay(bar_tm)
                    intraday_result = broker.run_risk_overlay_intraday(bar_tm)
                    if inspect.isawaitable(intraday_result):
                        await intraday_result

            # Close the last day
            if last_trade_day is not None:
                close_tm = calendar.replace_time(last_trade_day, 15, 30)
                self._clock.set_now(close_tm)
                broker.set_clock(close_tm)
                strategy._current_time = close_tm
                await strategy.on_day_close(close_tm)

            # 收尾清仓放在 try 内：失败时同样记录错误日志
            await broker.stop_backtest()

        except Exception as exc:
            self.logger.exception("Backtest failed: {}", exc)
            failure_tm = strategy._current_time or broker._clock
            record_backtest_log(
                portfolio_id=portfolio_id,
                level="ERROR",
                source="runner",
                message=f"回测失败：{exc}",
                dt=failure_tm,
                save_to_file=save_logs,
            )
            raise

        finally:
            await strategy.on_stop()

        self.logger.info(f"Backtest finished: {portfolio_id}")
        finish_tm = strategy._current_time or broker._clock
        record_backtest_log(
            portfolio_id=portfolio_id,
            level="INFO",
            source="runner",
            message=f"回测结束：portfolio_id={portfolio_id}",
            dt=finish_tm,
            save_to_file=save_logs,
        )

        # 4. Metrics
        stats = metrics(portfolio_id, start=start_date, end=end_date)

        return {
            "portfolio_id": portfolio_id,
            "metrics": stats.to_dict() if stats is not None else {},
        }
