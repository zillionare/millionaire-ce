"""30分钟行情存储类。

使用 Parquet 文件存储，按年分区。
字段：date, asset, open, high, low, close, volume, amount
不包含复权因子、涨跌停、ST 等辅助字段；读取时需单独关联。

时间索引为 **bar 收盘时刻**（10:00 表示 09:30-10:00），与项目 Calendar
一致。集合竞价/实时行情只写内存，不落盘；历史与每日回补均由 BaoStock 负责。
"""

import datetime
from pathlib import Path

import polars as pl

from quantide.data.models.calendar import Calendar
from quantide.data.stores.base import ParquetStorage, _as_datetime_bound


class MinuteBarsStore(ParquetStorage):
    """30分钟行情存储类。

    使用 Parquet 文件存储，按年分区。
    字段：date, asset, open, high, low, close, volume, amount

    重要：本存储只保存原始未复权数据，不包含 adjust/is_st/up_limit/down_limit。
    读取时需通过 DailyBars 关联复权因子与涨跌停，或由调用方自行处理。
    """

    def __init__(self, path: str | Path, calendar: Calendar):
        path = Path(path).expanduser()
        if path.suffix == ".parquet":
            partition_by = None
        else:
            partition_by = "year"

        super().__init__(
            "MinuteBars",
            path,
            calendar,
            None,
            error_handler=None,
            partition_by=partition_by,
        )
        self._id_cols = ["date", "asset"]

    def get(
        self,
        assets: list[str] | None = None,
        start: datetime.date | datetime.datetime | None = None,
        end: datetime.date | datetime.datetime | None = None,
        eager_mode: bool = True,
    ) -> pl.DataFrame | pl.LazyFrame:
        """获取30分钟行情数据（原始未复权）。

        Args:
            assets: 资产列表
            start: 开始日期
            end: 结束日期
            eager_mode: 是否立即执行

        Returns:
            pl.DataFrame 或 pl.LazyFrame，列：date, asset, open, high, low, close, volume, amount
        """
        lf = self._scan_store(keep_partition_col=False)
        if lf is None:
            return pl.DataFrame() if eager_mode else pl.LazyFrame()

        if start is not None:
            lf = lf.filter(pl.col("date") >= _as_datetime_bound(start))
        if end is not None:
            if isinstance(end, datetime.datetime):
                end_bound = end
            else:
                end_bound = datetime.datetime.combine(end, datetime.time.max)
            lf = lf.filter(pl.col("date") <= pl.lit(end_bound))

        if assets is not None:
            lf = lf.filter(pl.col("asset").is_in(assets))

        if eager_mode:
            return lf.collect()
        return lf

    def fetch(
        self,
        asset: str,
        start: datetime.date,
        end: datetime.date,
        progress_callback=None,
    ) -> int:
        """获取并保存指数行情数据。

        该方法已从主体移除，请使用 DataSyncService 统一调度。
        """
        raise RuntimeError("指数抓取功能已从 quantide 主体移除。")

    def last_dates(self) -> dict[str, datetime.date]:
        """每只标的已入库的最后日期（用于按标的增量回补）。"""
        lazy = self._scan_store(keep_partition_col=False)
        if lazy is None:
            return {}
        df = (
            lazy.with_columns(pl.col("date").cast(pl.Date))
            .group_by("asset")
            .agg(pl.col("date").max().alias("last"))
            .collect()
        )
        return {
            str(asset): last
            for asset, last in zip(df["asset"].to_list(), df["last"].to_list(), strict=True)
            if last is not None
        }

    def rec_counts_per_date(
        self, start: datetime.date | None = None, end: datetime.date | None = None
    ) -> dict[datetime.date, int]:
        """获取每个交易日期的记录数量统计。"""
        lazy = self._scan_store(keep_partition_col=False)
        if lazy is None:
            return {}

        lazy = lazy.with_columns(pl.col("date").cast(pl.Date))

        if start is not None:
            lazy = lazy.filter(pl.col("date").dt.strftime("%F") >= start.isoformat())
        if end is not None:
            lazy = lazy.filter(pl.col("date").dt.strftime("%F") <= end.isoformat())

        df = lazy.group_by("date").agg(pl.len().alias("n")).collect()
        dates = df["date"].to_list()
        counts = df["n"].to_list()
        result: dict[datetime.date, int] = {}
        for d, c in zip(dates, counts, strict=True):
            result[d] = int(c)
        return result
