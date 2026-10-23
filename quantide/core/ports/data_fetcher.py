"""标准化外部数据源端口 (DataFetcherPort)。

所有历史数据同步（日线、市值、涨跌停、ST、指数成分等）只依赖本端口，
不直接依赖具体数据源（tushare / akshare / baostock / 本地文件等）。

实现本端口的类必须把原始字段映射到下面的标准字段集，并保证：
- 返回的 DataFrame 列名使用标准名（见 `STANDARD_BAR_COLUMNS` 等）；
- 日期列一律为 `datetime64[ms]`（或已投递到存储层前转为 `pl.Date`）；
- 无数据时返回空 DataFrame（带齐标准列），而非 `None`；
- 逐条错误记录到 `errors`（`[func_name, identifier, message]`），不抛出未捕获异常。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable
from typing import Protocol

import pandas as pd

#: 标准扩展日线字段（顺序即存储列顺序）
STANDARD_BAR_COLUMNS: list[str] = [
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

#: 标准指数日线字段
STANDARD_INDEX_BAR_COLUMNS: list[str] = [
    "date",
    "index_code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
]

#: 标准指数成分字段（point-in-time）
STANDARD_INDEX_WEIGHT_COLUMNS: list[str] = [
    "date",
    "index_code",
    "con_code",
    "weight",
]

#: 标准股票列表字段
STANDARD_STOCK_LIST_COLUMNS: list[str] = [
    "asset",
    "name",
    "pinyin",
    "list_date",
    "delist_date",
]

#: 标准分钟线字段（``date`` 为 datetime64[ms]，含时分）
STANDARD_MINUTE_BAR_COLUMNS: list[str] = [
    "date",
    "asset",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
]

#: 错误记录格式：[func_name, identifier, message]
ErrorRecord = list[list]


class DataFetcherPort(Protocol):
    """外部数据源适配器端口。

    实现类可按需声明类属性 ``source_name``（如 "tushare" / "akshare"），供
    注册表与日志使用。
    """

    # ------------------------------------------------------------------
    # 基础元数据
    # ------------------------------------------------------------------
    # 基础元数据
    # ------------------------------------------------------------------
    def fetch_calendar(self, epoch: datetime.date) -> pd.DataFrame:
        """获取交易日历。

        Args:
            epoch: 起始日期，获取从该日期起的所有交易日。

        Returns:
            列含至少 ``date``（datetime.date）与 ``is_open``。
        """
        ...

    def fetch_stock_list(self) -> pd.DataFrame | None:
        """获取股票列表。

        Returns:
            列含 ``asset``/``name``/``list_date``/``delist_date``；无数据返回空 DataFrame 或 None。
        """
        ...

    # ------------------------------------------------------------------
    # 个股日线与辅助字段
    # ------------------------------------------------------------------
    def fetch_adjust_factor(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取复权因子。

        Returns:
            列含 ``date``/``asset``/``adjust``。
        """
        ...

    def fetch_bars(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取基础日线（未复权）。

        Returns:
            列含 ``date``/``asset``/``open``/``high``/``low``/``close``/``volume``/``amount``。
        """
        ...

    def fetch_limit_price(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取涨跌停价。

        Returns:
            列含 ``date``/``asset``/``up_limit``/``down_limit``。
        """
        ...

    def fetch_st_info(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取 ST 标记。

        Returns:
            列含 ``date``/``asset``/``is_st``。
        """
        ...

    def fetch_bars_ext(
        self,
        dates: Iterable[datetime.date] | datetime.date,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取标准化扩展日线数据。

        合并 bars / adjust / limit / st，返回列完全等于 :data:`STANDARD_BAR_COLUMNS`。
        缺失字段按缺省值填充（adjust=1, is_st=False, limit=0）。

        Args:
            dates: 交易日列表或单日。
            phase_callback: 各抓取阶段的进度回调。

        Returns:
            标准化扩展日线 DataFrame 与错误记录。
        """
        ...

    def fetch_daily_basic(
        self,
        dates: Iterable[datetime.date] | datetime.date,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取每日市值等基本面数据。

        Returns:
            列含 ``date``/``asset``/``total_mv``/``circ_mv``。
        """
        ...

    # ------------------------------------------------------------------
    # 指数（基准 / 超额 / 选股池）
    # ------------------------------------------------------------------
    def fetch_index_bars(
        self,
        index_codes: Iterable[str] | str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取指数日线行情（基准/超额分析用）。

        Returns:
            列等于 :data:`STANDARD_INDEX_BAR_COLUMNS`。
        """
        ...

    def fetch_index_weights(
        self,
        index_codes: Iterable[str] | str,
        snapshot_dates: Iterable[datetime.date],
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """按快照日获取指数成分（point-in-time, 无前视）。

        Returns:
            列等于 :data:`STANDARD_INDEX_WEIGHT_COLUMNS`。
        """
        ...

    # ------------------------------------------------------------------
    # 分钟线 / 实时行情（P0 实盘/回测用）
    # ------------------------------------------------------------------
    def fetch_minute_bars(
        self,
        asset: str,
        start_date: datetime.date | None = None,
        end_date: datetime.date | None = None,
        period: str = "30",
        count: int = 320,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取分钟线行情。

        Args:
            asset: 证券代码
            start_date: 起始日
            end_date: 结束日
            period: 周期，"30" 表示 30 分钟
            count: 最多返回多少根

        Returns:
            列等于 :data:`STANDARD_MINUTE_BAR_COLUMNS`；无数据返回空 DataFrame。
        """
        ...

    def fetch_quote_snapshot(
        self,
        symbols: Iterable[str] | str,
    ) -> dict[str, dict]:
        """获取实时行情快照。

        Returns:
            {symbol: {"price", "open", "high", "low", "volume", "amount",
            "up_limit", "down_limit", "ts"}}。缺失字段为 None。
        """
        ...

    # ------------------------------------------------------------------
    # 分钟线 / 实时快照（分钟线仅用于模拟盘与 30 分钟策略，可不落盘）
    # ------------------------------------------------------------------
    def fetch_minute_bars(
        self,
        asset: str,
        start_date: datetime.date | None = None,
        end_date: datetime.date | None = None,
        period: str = "30",
        count: int = 320,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        """获取分钟线。

        Args:
            asset: 证券代码（``600519.SH``）。
            start_date: 起始日；None 表示不限。
            end_date: 结束日；None 表示不限。
            period: 周期，``1``/``5``/``15``/``30``/``60``（分钟）。
            count: 最多返回多少根（数据源单次上限内）。

        Returns:
            列等于 :data:`STANDARD_MINUTE_BAR_COLUMNS`；无数据返回空 DataFrame。
        """
        ...

    def fetch_quote_snapshot(
        self, symbols: Iterable[str] | str
    ) -> dict[str, dict]:
        """获取实时行情快照。

        Returns:
            ``{symbol: {price, open, high, low, volume, amount,
            prev_close, up_limit, down_limit, ts}}``；缺失字段为 None。
        """
        ...
