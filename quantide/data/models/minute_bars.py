"""30 分钟行情读取层。

存储层只保存原始 OHLCV；本模块负责在读取时关联**日线复权因子、ST、涨跌停**，
使回测/策略可以直接拿到可用的复权分钟线。

时间约定：`date` 是 **bar 起点**（如 09:30 表示 09:30-10:00），
不同数据源（腾讯/baostock）在写入前统一到该口径。
"""

from __future__ import annotations

import datetime

import polars as pl
from loguru import logger

from quantide.core.enums import FrameType
from quantide.data.helper import qfq_adjustment
from quantide.data.models.calendar import calendar
from quantide.data.models.daily_bars import daily_bars
from quantide.data.stores.minute_bars import MinuteBarsStore

MINUTE_BAR_COLUMNS = [
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


class MinuteBars:
    """分钟线读取模型（关联日线辅助字段）。"""

    def __init__(self) -> None:
        self._store: MinuteBarsStore | None = None

    def connect(self, store: MinuteBarsStore) -> MinuteBars:
        """绑定底层存储。"""
        self._store = store
        return self

    @property
    def store(self) -> MinuteBarsStore:
        if self._store is None:
            raise RuntimeError("MinuteBars 未初始化，请先调用 init_data()")
        return self._store

    def get_bars_in_range(
        self,
        start: datetime.date | datetime.datetime,
        end: datetime.date | datetime.datetime | None = None,
        assets: list[str] | None = None,
        adjust: str | None = "qfq",
        eager_mode: bool = True,
    ) -> pl.DataFrame | pl.LazyFrame:
        """读取区间内的分钟线，并关联复权因子/ST/涨跌停。

        Args:
            start: 起始日（含）。
            end: 结束日（含）；None 表示最后一天。
            assets: 资产列表；None 表示全部。
            adjust: ``qfq``/``hfq``/None。分钟线需要复权时建议 qfq。
            eager_mode: 是否立即执行。

        Returns:
            列见 ``MINUTE_BAR_COLUMNS``。无数据返回空 DataFrame。
        """
        if self._store is None:
            return pl.DataFrame()
        raw = self._store.get(assets, start, end, eager_mode=True)
        if raw is None or raw.is_empty():
            return pl.DataFrame()

        raw = raw.with_columns(pl.col("date").cast(pl.Datetime("ms")))
        start_date = (
            start.date() if isinstance(start, datetime.datetime) else start
        )
        end_date = (
            end.date() if isinstance(end, datetime.datetime) else end
        )
        if end_date is None:
            end_date = raw["date"].max().date()

        # 日线辅助字段：adjust / is_st / up_limit / down_limit
        aux = daily_bars.get_bars_in_range(
            start_date, end_date, assets=assets, adjust=None, eager_mode=True
        )
        if isinstance(aux, pl.DataFrame) and not aux.is_empty():
            aux = (
                aux.with_columns(pl.col("date").cast(pl.Date).alias("_day"))
                .select(
                    [
                        "_day",
                        "asset",
                        "adjust",
                        "is_st",
                        "up_limit",
                        "down_limit",
                    ]
                )
                .unique(subset=["_day", "asset"], keep="last")
            )
            merged = (
                raw.with_columns(pl.col("date").dt.date().alias("_day"))
                .join(aux, on=["_day", "asset"], how="left")
                .drop("_day")
            )
        else:
            logger.warning(
                "分钟线缺少对应日线辅助字段（复权/涨跌停），按未复权处理: {}",
                raw["date"].min(),
            )
            merged = raw.with_columns(
                pl.lit(1.0).alias("adjust"),
                pl.lit(False).alias("is_st"),
                pl.lit(0.0).alias("up_limit"),
                pl.lit(0.0).alias("down_limit"),
            )

        merged = merged.with_columns(
            pl.col("adjust").fill_null(1.0).cast(pl.Float64),
            pl.col("is_st").fill_null(False).cast(pl.Boolean),
            pl.col("up_limit").fill_null(0.0).cast(pl.Float64),
            pl.col("down_limit").fill_null(0.0).cast(pl.Float64),
        ).sort(["asset", "date"])

        if adjust in ("qfq", "hfq"):
            # 涨跌停价也按同一比例调整，保证与复权后的 close 可比
            # （qfq 用最后因子、hfq 用首个因子）。
            base = (
                pl.col("adjust").last().over("asset")
                if adjust == "qfq"
                else pl.col("adjust").first().over("asset")
            )
            ratio = pl.col("adjust") / base
            merged = merged.with_columns(
                (pl.col("up_limit") * ratio).alias("up_limit"),
                (pl.col("down_limit") * ratio).alias("down_limit"),
            )
            if adjust == "qfq":
                merged = qfq_adjustment(merged, eager_mode=True)
            else:
                from quantide.data.helper import hfq_adjustment

                merged = hfq_adjustment(merged, eager_mode=True)
            assert isinstance(merged, pl.DataFrame)

        result = merged.select(MINUTE_BAR_COLUMNS).sort(["date", "asset"])
        if eager_mode:
            return result
        return result.lazy()

    def get_bars(
        self,
        n: int,
        end: datetime.date | datetime.datetime | None = None,
        assets: list[str] | None = None,
        adjust: str | None = "qfq",
        eager_mode: bool = True,
    ) -> pl.DataFrame | pl.LazyFrame:
        """读取最近 n 根分钟线。"""
        if end is None:
            end = datetime.datetime.now()
        end_date = end.date() if isinstance(end, datetime.datetime) else end
        # 30 分钟线每个交易日约 8 根；多取几天缓冲假期与停牌。
        lookback_days = max(2, (int(n) + 7) // 8 + 5)
        start_date = calendar.shift(end_date, -lookback_days, FrameType.DAY)
        frame = self.get_bars_in_range(
            start_date, end_date, assets=assets, adjust=adjust, eager_mode=True
        )
        assert isinstance(frame, pl.DataFrame)
        if frame.is_empty():
            return frame
        frame = (
            frame.sort("date").group_by("asset").tail(int(n)).sort(["date", "asset"])
        )
        if eager_mode:
            return frame
        return frame.lazy()


minute_bars = MinuteBars()

__all__ = ["MinuteBars", "minute_bars", "MINUTE_BAR_COLUMNS"]
