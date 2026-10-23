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

    @staticmethod
    def _minute_period(frame_type: str) -> str | None:
        """把 frame_type 映射为分钟周期；非分钟返回 None。"""
        text = str(frame_type or "").strip().lower()
        if text in {"30m", "30", "m30", "30min"}:
            return "30"
        if text in {"60m", "60", "m60", "60min", "1h"}:
            return "60"
        if text in {"15m", "15", "m15", "15min"}:
            return "15"
        if text in {"5m", "5", "m5", "5min"}:
            return "5"
        if text in {"1m", "1", "m1", "1min"}:
            return "1"
        return None

    @staticmethod
    def _bucket_start(tm: datetime.datetime, period_minutes: int = 30) -> datetime.datetime:
        """返回 A 股交易时段内分钟 bar 的起点（向下取整）。"""
        session_start = datetime.time(9, 30)
        if tm.time() < session_start:
            return datetime.datetime.combine(tm.date(), session_start)
        elapsed = (
            datetime.datetime.combine(tm.date(), tm.time())
            - datetime.datetime.combine(tm.date(), session_start)
        ).total_seconds() / 60.0
        period = max(1, int(period_minutes))
        if elapsed <= 120:
            index = int(max(0.0, elapsed) // period)
            return datetime.datetime.combine(
                tm.date(), session_start
            ) + datetime.timedelta(minutes=index * period)
        # 午休 11:30-13:00（90 分钟）不计入交易分钟
        afternoon = max(0.0, elapsed - 120 - 90)
        index = int(afternoon // period)
        afternoon_start = datetime.datetime.combine(tm.date(), datetime.time(13, 0))
        return afternoon_start + datetime.timedelta(minutes=index * period)

    def get_minute_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        period: str = "30",
        include_forming_bar: bool = False,
    ) -> pl.DataFrame:
        """最近 n 根已完成分钟线（已关联复权因子/ST/涨跌停）。"""
        from quantide.data.models.minute_bars import minute_bars

        end_date = self._resolve_end_date(end_dt)
        frame = minute_bars.get_bars(
            count, end=end_date, assets=[asset], adjust="qfq"
        )
        return frame

    def get_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        frame_type: str = "1d",
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        """最近 n 根已完成 bar（日线或分钟线）。"""
        period = self._minute_period(frame_type)
        if period is not None:
            return self.get_minute_history(
                asset, count, end_dt, period=period,
                include_forming_bar=False,
            )
        if frame_type != "1d":
            raise NotImplementedError(
                f"BarView 不支持的历史周期: {frame_type}"
            )
        end_date = self._resolve_end_date(end_dt)
        return self._fetch_completed(asset, count, end_date, frame_type)

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

    def __init__(self, bars=None, market_data=None, today_func=None):
        super().__init__(bars)
        self._market_data = market_data
        self._today_func = today_func

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

    def _inject_forming_minute(
        self,
        completed: pl.DataFrame,
        end_dt: datetime.datetime,
        asset: str,
        period: str,
    ) -> pl.DataFrame:
        """把当前 30 分钟桶的 forming bar 合进已完成分钟线。

        成交量/成交额为「当日累计值减去今日已完成桶的合计」，近似当前桶增量；
        open 用上一根完成 bar 的 close（当日第一桶用当日开盘价），
        high/low 取 open 与现价的最大/最小。
        """
        snapshot = self._snapshot_for(asset)
        if snapshot is None:
            return completed
        bucket = self._bucket_start(end_dt, int(period))
        if completed is None or completed.is_empty():
            return completed
        today = bucket.date()
        today_rows = completed.filter(pl.col("date").dt.date() == today)
        latest_adj = 1.0
        is_st = False
        up_limit = 0.0
        down_limit = 0.0
        last_close = None
        raw_volume_sum = 0.0
        amount_sum = 0.0
        if not today_rows.is_empty():
            last = today_rows.sort("date").tail(1).row(0, named=True)
            latest_adj = float(last.get("adjust") or 1.0)
            is_st = bool(last.get("is_st") or False)
            up_limit = float(last.get("up_limit") or 0.0)
            down_limit = float(last.get("down_limit") or 0.0)
            last_close = float(last.get("close") or 0.0)
            latest_factor = pl.col("adjust").last()
            raw = today_rows.with_columns(
                (pl.col("volume") * pl.col("adjust") / latest_factor).alias(
                    "_raw_volume"
                ),
                pl.col("amount").alias("_amount"),
            )
            raw_volume_sum = float(raw["_raw_volume"].sum() or 0.0)
            amount_sum = float(raw["_amount"].sum() or 0.0)
        price = float(snapshot.get("close") or snapshot.get("price") or 0.0)
        if price <= 0:
            return completed
        open_price = last_close if last_close and last_close > 0 else price
        bucket_volume = max(
            0.0, float(snapshot.get("volume") or 0.0) - raw_volume_sum
        )
        bucket_amount = max(
            0.0, float(snapshot.get("amount") or 0.0) - amount_sum
        )
        forming = pl.DataFrame(
            [
                {
                    "date": bucket,
                    "asset": asset,
                    "open": open_price,
                    "high": max(open_price, price),
                    "low": min(open_price, price),
                    "close": price,
                    "volume": bucket_volume,
                    "amount": bucket_amount,
                    "adjust": latest_adj,
                    "is_st": is_st,
                    "up_limit": up_limit,
                    "down_limit": down_limit,
                }
            ]
        ).with_columns(pl.col("date").cast(pl.Datetime("ms")))
        merged = pl.concat([completed, forming], how="vertical_relaxed")
        merged = merged.unique(subset=["date", "asset"], keep="last")
        return merged.sort(["date", "asset"])

    def get_minute_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        period: str = "30",
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        completed = super().get_minute_history(
            asset, count, end_dt, period=period, include_forming_bar=False
        )
        if (
            include_forming_bar
            and end_dt is not None
            and end_dt.date() == self._today()
            and end_dt.time() > datetime.time(9, 30)
        ):
            completed = self._inject_forming_minute(
                completed, end_dt, asset, period
            )
        return completed

    def get_history(
        self,
        asset: str,
        count: int,
        end_dt: datetime.datetime | None = None,
        frame_type: str = "1d",
        include_forming_bar: bool = True,
    ) -> pl.DataFrame:
        period = self._minute_period(frame_type)
        if period is not None:
            return self.get_minute_history(
                asset, count, end_dt, period=period,
                include_forming_bar=include_forming_bar,
            )
        if frame_type != "1d":
            raise NotImplementedError(
                f"LiveBarView 不支持的历史周期: {frame_type}"
            )
        end_date = self._resolve_end_date(end_dt)
        if not include_forming_bar and end_date == self._today():
            end_date = self._prev_trade_date(end_date)
        completed = self._fetch_completed(asset, count, end_date, frame_type)
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
