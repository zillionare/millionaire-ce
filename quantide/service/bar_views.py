"""股票日线“视图”层。

把**已完结的日线**（持久化在 `daily_bars`）与**盘中 forming bar**（行情订阅中心/
实时行情源的最新快照）统一在一个接口下提供：

- `HistBarView`：仅已完结日线，用于回测；
- `LiveBarView`：已完结日线 + 今日 forming bar，用于 Paper 实盘。

设计原则：
1. 数据层不做任何时点冻结，也不落盘 forming bar；形成 bar 只在内存中拼装，
   策略按需一次性取走；
2. 只有当 `end_dt` 落在“今日且晚于 09:30”时才会注入 forming bar，否则与
   已完结日线完全一致；
3. 回测与实盘走同一套代码路径，回测侧永远不注入 forming bar。
"""

from __future__ import annotations

import datetime
from typing import Any

import polars as pl

from quantide.core.enums import FrameType

STANDARD_BAR_COLUMNS = [
    "date",
    "asset",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adjust",
    "is_st",
    "up_limit",
    "down_limit",
]


class BarViewError(Exception):
    """行情视图错误。"""


class HistBarView:
    """已完结日线视图基类。

    提供两种读取方式：
    - `get_history`：单只股票的最近 n 根日线（策略 `get_bars` 走此路径）；
    - `get_bars_range`：多只股票的日期区间日线（策略面板选股/预取走此路径）。
    """

    def __init__(self, bars=None):
        from quantide.data.models.daily_bars import daily_bars

        self._bars = bars if bars is not None else daily_bars

    def _resolve_end_date(self, end_dt):
        end_date = end_dt.date() if isinstance(end_dt, datetime.datetime) else end_dt
        if isinstance(end_dt, datetime.datetime) and end_dt.time() <= datetime.time(9, 30):
            try:
                from quantide.data.models.calendar import calendar

                return calendar.day_shift(end_date, -1)
            except Exception:
                return end_date - datetime.timedelta(days=1)
        return end_date

    def _fetch_completed(self, asset, count, end_date, frame_type="1d"):
        return self._bars.get_bars(
            n=count, end=end_date, assets=[asset], adjust=None
        )

    #: 兼容历史字符串写法
    _MINUTE_ALIASES = {
        "m30": FrameType.MIN30,
        "30": FrameType.MIN30,
        "30min": FrameType.MIN30,
        "m60": FrameType.MIN60,
        "60": FrameType.MIN60,
        "60min": FrameType.MIN60,
        "1h": FrameType.MIN60,
        "m15": FrameType.MIN15,
        "15": FrameType.MIN15,
        "15min": FrameType.MIN15,
        "m5": FrameType.MIN5,
        "5": FrameType.MIN5,
        "5min": FrameType.MIN5,
        "m1": FrameType.MIN1,
        "1": FrameType.MIN1,
        "1min": FrameType.MIN1,
    }

    @classmethod
    def _frame_type(cls, frame_type: FrameType | str | None) -> FrameType:
        """把字符串/枚举统一成 FrameType。"""
        if isinstance(frame_type, FrameType):
            return frame_type
        text = str(frame_type or "").strip()
        if not text:
            return FrameType.DAY
        lowered = text.lower()
        if lowered in cls._MINUTE_ALIASES:
            return cls._MINUTE_ALIASES[lowered]
        try:
            return FrameType(text)
        except ValueError as exc:
            raise NotImplementedError(f"BarView 不支持的周期: {frame_type}") from exc

    @classmethod
    def _minute_frame_type(
        cls, frame_type: FrameType | str | None
    ) -> FrameType | None:
        """分钟周期返回对应 FrameType，否则返回 None。"""
        from quantide.data.models.calendar import calendar

        ft = cls._frame_type(frame_type)
        return ft if ft in calendar.minute_level_frames else None

    @staticmethod
    def _bucket_end(
        now: datetime.datetime, frame_type: FrameType = FrameType.MIN30
    ) -> datetime.datetime:
        """当前未完成桶的收盘时刻（Calendar 口径）。"""
        from quantide.data.models.calendar import calendar

        end = calendar.ceiling(now, frame_type)
        return end.replace(tzinfo=None) if end.tzinfo is not None else end

    def get_minute_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        frame_type: FrameType | str = FrameType.MIN30,
        include_forming_bar: bool = False,
    ) -> pl.DataFrame:
        """最近 n 根已完成分钟线（已关联复权因子/ST/涨跌停）。"""
        ft = self._minute_frame_type(frame_type)
        if ft is None:
            raise NotImplementedError(f"BarView 不是分钟周期: {frame_type}")
        if end_dt is None:
            end_dt = datetime.datetime.now()
        if end_dt.tzinfo is not None:
            end_dt = end_dt.replace(tzinfo=None)
        from quantide.data.models.minute_bars import minute_bars

        return minute_bars.get_bars(
            count, end=end_dt, assets=[asset], adjust="qfq"
        )

    def get_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        frame_type: FrameType | str = FrameType.DAY,
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        """最近 n 根已完成 bar（日线或分钟线）。"""
        ft = self._frame_type(frame_type)
        if self._minute_frame_type(ft) is not None:
            return self.get_minute_history(
                asset, count, end_dt, frame_type=ft,
                include_forming_bar=False,
            )
        if ft is not FrameType.DAY:
            raise NotImplementedError(
                f"BarView 不支持的历史周期: {frame_type}"
            )
        end_date = self._resolve_end_date(end_dt)
        return self._fetch_completed(asset, count, end_date, ft.value)

    def get_bars_range(
        self,
        assets: list[str] | None,
        start_dt: datetime.datetime | datetime.date,
        end_dt: datetime.datetime | datetime.date | None = None,
        adjust: str | None = None,
        include_forming_bar: bool = False,
    ) -> pl.DataFrame:
        """指定日期区间的已完成日线（批量，一次扫描）。"""
        if end_dt is None:
            end_dt = start_dt
        end_date = end_dt.date() if isinstance(end_dt, datetime.datetime) else end_dt
        start_date = start_dt.date() if isinstance(start_dt, datetime.datetime) else start_dt
        df = self._bars.get_bars_in_range(
            start_date, end_date, assets=assets, adjust=adjust, eager_mode=True
        )
        if df is None or df.is_empty():
            return pl.DataFrame()
        return df.sort(["date", "asset"])


class LiveBarView(HistBarView):
    """带“收盘集合竞价 forming bar”的日线视图，用于 Paper 实盘。

    形成 bar 来自行情订阅中心（`market_data.snapshot`），没有订阅中心时
    回落 `live_quote.get_daily_bar`（网关/直连行情）。
    """

    def __init__(
        self,
        bars=None,
        market_data=None,
        today_func=None,
        minute_aggregator=None,
    ):
        super().__init__(bars)
        self._market_data = market_data
        self._today_func = today_func
        self._minute_aggregator = minute_aggregator

    @property
    def _aggregator(self):
        """盘中 30 分钟线合成器（默认全局单例）。"""
        if self._minute_aggregator is not None:
            return self._minute_aggregator
        from quantide.service.minute_bar_aggregator import minute_bar_aggregator

        return minute_bar_aggregator

    def _limits(self, asset: str) -> tuple[float, float]:
        """今日涨跌停价：优先行情端口，其次订阅中心。"""
        market_data = self._market_data
        if market_data is not None and hasattr(market_data, "get_limits"):
            try:
                return market_data.get_limits(asset)
            except Exception:  # pragma: no cover - 行情端口异常时回落
                pass
        from quantide.service.market_data_hub import market_data_hub

        return market_data_hub.get_limits(asset)

    def _today(self) -> datetime.date:
        if self._today_func is not None:
            return self._today_func()
        return datetime.datetime.now().date()

    def _snapshot_for(self, asset: str) -> dict[str, Any] | None:
        """把一只股票的今日行情快照转成 forming bar 字典。"""
        snapshot = None
        if self._market_data is not None and hasattr(self._market_data, "snapshot"):
            try:
                snap = self._market_data.snapshot([asset]).get(asset)
            except Exception:
                snap = None
            if isinstance(snap, dict):
                snapshot = snap
            elif snap is not None:
                snapshot = {
                    field: getattr(snap, field, None)
                    for field in (
                        "price",
                        "open",
                        "high",
                        "low",
                        "volume",
                        "amount",
                        "ts",
                    )
                }
        if snapshot is None:
            from quantide.service.livequote import live_quote

            bar = live_quote.get_daily_bar(asset)
            if bar is None:
                return None
            snapshot = bar
        price = snapshot.get("price") if isinstance(snapshot, dict) else None
        if price is None or price <= 0:
            price = snapshot.get("close") if isinstance(snapshot, dict) else None
        if price is None or price <= 0:
            return None
        ts = snapshot.get("ts") if isinstance(snapshot, dict) else None
        if ts is None:
            ts = snapshot.get("dt") if isinstance(snapshot, dict) else None
        if ts is not None:
            ts_date = ts.date() if hasattr(ts, "date") else ts
            if isinstance(ts_date, datetime.datetime):
                ts_date = ts_date.date()
            if ts_date != self._today():
                return None
        return {
            "date": self._today(),
            "asset": asset,
            "open": snapshot.get("open") or price,
            "high": snapshot.get("high") or price,
            "low": snapshot.get("low") or price,
            "close": price,
            "volume": snapshot.get("volume") or 0,
            "amount": snapshot.get("amount") or 0,
        }

    def _forming_rows(self, assets: list[str], today: datetime.date) -> pl.DataFrame:
        """把今日各标的的 forming bar 拼成 DataFrame。缺行情的标的直接跳过。"""
        rows: list[dict[str, Any]] = []
        for asset in assets:
            snap = self._snapshot_for(asset)
            if snap is not None:
                rows.append(snap)
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows)

    def _prev_trade_date(self, today: datetime.date) -> datetime.date:
        try:
            from quantide.data.models.calendar import calendar

            return calendar.day_shift(today, -1)
        except Exception:
            return today - datetime.timedelta(days=1)

    def _fetch_completed(self, asset, count, end_date, frame_type="1d"):
        # 优先使用行情端口自身的历史行情（网关等），回落 daily_bars 存储
        if self._market_data is not None and hasattr(self._market_data, "get_history"):
            return self._market_data.get_history(asset, count, end_date, frame_type)
        if self._market_data is not None and hasattr(self._market_data, "get_bars"):
            return self._market_data.get_bars(
                n=count, end=end_date, assets=[asset], adjust=None
            )
        bars_get = getattr(self._bars, "get_bars", None)
        if bars_get is not None:
            return bars_get(
                n=count, end=end_date, assets=[asset], adjust=None
            )
        return pl.DataFrame()

    def _inject_forming(
        self,
        completed: pl.DataFrame,
        end_dt: datetime.datetime | None,
        assets: list[str] | None,
        include_forming_bar: bool,
    ) -> pl.DataFrame:
        """把今日 forming bar 合进已完成日线；没有行情时返回已完成日线。"""
        if (
            not include_forming_bar
            or end_dt is None
            or end_dt.date() != self._today()
            or end_dt.time() <= datetime.time(9, 30)
        ):
            return completed
        today = self._today()
        date_col = "date" if "date" in completed.columns else ("frame" if "frame" in completed.columns else None)
        if date_col is None:
            return completed
        if date_col != "date":
            completed = completed.rename({date_col: "date"})
        assets = assets or completed["asset"].unique().to_list()
        forming = self._forming_rows(assets, today)
        if forming.is_empty():
            # 没有今日行情：按“昨日结束”处理，丢掉已完成部分的今日行（若有）
            completed = completed.filter(pl.col("date") < today)
            return completed
        forming = forming.with_columns(pl.col("date").cast(pl.Date))
        merged = pl.concat([completed, forming], how="vertical_relaxed")
        completed = merged.unique(subset=["date", "asset"], keep="last").sort(["date", "asset"])
        return completed

    def _merge_aggregated_minute(
        self,
        completed: pl.DataFrame,
        asset: str,
        end_dt: datetime.datetime,
    ) -> pl.DataFrame:
        """把内存合成的当日已完成桶合进 BaoStock 历史（历史优先）。"""
        aggregator = self._aggregator
        try:
            aggregator.subscribe([asset])
            rows = aggregator.completed(asset, end=end_dt)
        except Exception:  # pragma: no cover - 合成器不可用时只返回历史
            return completed
        if not rows:
            return completed
        if completed is not None and not completed.is_empty():
            existing = set(completed["date"].to_list())
            rows = [row for row in rows if row["date"] not in existing]
        if not rows:
            return completed
        up_limit, down_limit = self._limits(asset)
        today = pl.DataFrame(rows).with_columns(
            pl.col("date").cast(pl.Datetime("ms")),
            pl.lit(1.0).alias("adjust"),
            pl.lit(False).alias("is_st"),
            pl.lit(up_limit).alias("up_limit"),
            pl.lit(down_limit).alias("down_limit"),
        )
        if completed is None or completed.is_empty():
            return today
        today = today.select(completed.columns)
        return pl.concat([completed, today], how="vertical_relaxed")

    def _append_forming_minute(
        self,
        completed: pl.DataFrame,
        asset: str,
        end_dt: datetime.datetime,
    ) -> pl.DataFrame:
        """追加当前未完成桶（收盘时刻 > end_dt）。"""
        try:
            self._aggregator.subscribe([asset])
            row = self._aggregator.forming(asset, end_dt)
        except Exception:  # pragma: no cover - 合成器不可用时忽略
            return completed
        if row is None:
            return completed
        up_limit, down_limit = self._limits(asset)
        row = {
            **row,
            "adjust": 1.0,
            "is_st": False,
            "up_limit": up_limit,
            "down_limit": down_limit,
        }
        forming = pl.DataFrame([row]).with_columns(
            pl.col("date").cast(pl.Datetime("ms"))
        )
        if completed is None or completed.is_empty():
            return forming
        forming = forming.select(completed.columns)
        merged = pl.concat([completed, forming], how="vertical_relaxed")
        return merged.unique(subset=["date", "asset"], keep="last")

    def get_minute_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        frame_type: FrameType | str = FrameType.MIN30,
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        ft = self._minute_frame_type(frame_type)
        if ft is None:
            raise NotImplementedError(f"LiveBarView 不是分钟周期: {frame_type}")
        if end_dt is None:
            end_dt = datetime.datetime.now()
        if end_dt.tzinfo is not None:
            end_dt = end_dt.replace(tzinfo=None)
        completed = super().get_minute_history(
            asset, count, end_dt, frame_type=ft, include_forming_bar=False
        )
        completed = self._merge_aggregated_minute(completed, asset, end_dt)
        if include_forming_bar and end_dt.time() > datetime.time(9, 30):
            completed = self._append_forming_minute(completed, asset, end_dt)
        if completed is None or completed.is_empty():
            return completed
        return completed.sort(["date", "asset"]).tail(int(count))

    def get_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        frame_type: FrameType | str = FrameType.DAY,
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        ft = self._frame_type(frame_type)
        if self._minute_frame_type(ft) is not None:
            return self.get_minute_history(
                asset, count, end_dt, frame_type=ft,
                include_forming_bar=include_forming_bar,
            )
        if ft is not FrameType.DAY:
            raise NotImplementedError(
                f"LiveBarView 不支持的历史周期: {frame_type}"
            )
        end_date = self._resolve_end_date(end_dt)
        if not include_forming_bar and end_date == self._today():
            end_date = self._prev_trade_date(end_date)
        completed = self._fetch_completed(asset, count, end_date, ft.value)
        completed = self._inject_forming(completed, end_dt, [asset], include_forming_bar)
        return completed

    def get_bars_range(
        self,
        assets: list[str] | None,
        start_dt: datetime.datetime | datetime.date,
        end_dt: datetime.datetime | datetime.date | None = None,
        adjust: str | None = None,
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        if end_dt is None:
            end_dt = start_dt
        end_date = end_dt.date() if isinstance(end_dt, datetime.datetime) else end_dt
        start_date = start_dt.date() if isinstance(start_dt, datetime.datetime) else start_dt
        completed = self._bars.get_bars_in_range(
            start_date, end_date, assets=assets, adjust=adjust, eager_mode=True
        )
        if completed is None or completed.is_empty():
            return pl.DataFrame()
        completed = self._inject_forming(
            completed, end_dt, assets or completed["asset"].unique().to_list(), include_forming_bar
        )
        return completed.sort(["date", "asset"])
