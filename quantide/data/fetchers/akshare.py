"""Akshare 数据源适配器（参考实现）。

Akshare 是免费的开源金融数据接口库，适合作为 Tushare 的替代数据源。
本适配器把 Akshare 的原始字段映射到 :data:`quantide.core.ports.data_fetcher`
定义的标准字段集。

注意 Akshare 的能力边界：
- 不提供按交易日批量拉取全市场日线的接口；此处按股票逐支拉取区间日线，
  再在内存按目标交易日过滤，适合小范围回补，不适合全市场大批量同步；
- 不提供按日期批量的涨跌停 / ST / 复权因子 / 市值 / 指数成分 PIT 接口，
  对应方法返回空标准框架并记录警告（生产环境请继续使用 Tushare 取这些字段）。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable
from typing import Any

import pandas as pd

from quantide.core.ports.data_fetcher import (
    STANDARD_BAR_COLUMNS,
    STANDARD_INDEX_BAR_COLUMNS,
    STANDARD_INDEX_WEIGHT_COLUMNS,
    STANDARD_STOCK_LIST_COLUMNS,
    ErrorRecord,
)


def _require_akshare() -> Any:
    """导入 akshare，未安装时返回 None。"""
    try:
        import akshare as ak  # type: ignore[import-not-found]

        return ak
    except ImportError:  # pragma: no cover - 取决于环境
        return None


def _empty_bar_frame() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="object") for col in STANDARD_BAR_COLUMNS})


def _empty_index_bar_frame() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="object") for col in STANDARD_INDEX_BAR_COLUMNS})


def _empty_index_weight_frame() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="object") for col in STANDARD_INDEX_WEIGHT_COLUMNS})


def _empty_stock_list_frame() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="object") for col in STANDARD_STOCK_LIST_COLUMNS})


def _to_std_date(value: Any) -> Any:
    """把 Akshare 的日期字段统一为 datetime64[ms]。"""
    return pd.to_datetime(value).astype("datetime64[ms]")


def _to_tushare_asset(ak_code: str) -> str:
    """把 Akshare 股票代码（如 "600000"）转为 Tushare 风格（如 "600000.SH"）。"""
    code = str(ak_code).strip().split(".")[0]
    if len(code) == 6 and code[0] == "6":
        return f"{code}.SH"
    return f"{code}.SZ"


def _to_ak_index_code(index_code: str) -> str:
    """把 Tushare 风格指数代码转为 Akshare 风格（如 "000300.SH" -> "sh000300"）。"""
    code, market = str(index_code).split(".")
    prefix = "sh" if market.upper() == "SH" else "sz"
    return f"{prefix}{code}"


class AkshareDataFetcher:
    """基于 Akshare 的标准端口适配器。

    source_name = "akshare"。
    """

    source_name = "akshare"

    # ------------------------------------------------------------------
    # 基础元数据
    # ------------------------------------------------------------------
    def fetch_calendar(self, epoch: datetime.date) -> pd.DataFrame:
        ak = _require_akshare()
        if ak is None:
            return pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ms]"),
                    "is_open": pd.Series(dtype="int64"),
                }
            )
        try:
            df = ak.tool_trade_date_hist_sina()
        except Exception:  # pragma: no cover - 网络/接口异常
            return pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ms]"),
                    "is_open": pd.Series(dtype="int64"),
                }
            )
        if df is None or df.empty:
            return pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ms]"),
                    "is_open": pd.Series(dtype="int64"),
                }
            )
        date_col = "trade_date" if "trade_date" in df.columns else "date"
        df = df.rename(columns={date_col: "date"})
        df["date"] = _to_std_date(df["date"]).dt.date
        df["is_open"] = 1
        df = df[df["date"] >= epoch]
        return df[["date", "is_open"]].sort_values("date").reset_index(drop=True)

    def fetch_stock_list(self) -> pd.DataFrame | None:
        ak = _require_akshare()
        if ak is None:
            return None
        try:
            df = ak.stock_info_a_code_name()
        except Exception:  # pragma: no cover - 网络/接口异常
            return None
        if df is None or df.empty:
            return None
        code_col = "代码" if "代码" in df.columns else df.columns[0]
        name_col = "名称" if "名称" in df.columns else (df.columns[1] if len(df.columns) > 1 else code_col)
        out = pd.DataFrame()
        out["asset"] = df[code_col].astype(str).map(_to_tushare_asset)
        out["name"] = df[name_col].astype(str)
        if "上市日期" in df.columns:
            out["list_date"] = pd.to_datetime(df["上市日期"], errors="coerce").dt.date
        else:
            out["list_date"] = pd.NaT
        out["delist_date"] = pd.NaT
        out["pinyin"] = ""
        return out[STANDARD_STOCK_LIST_COLUMNS]

    # ------------------------------------------------------------------
    # 个股日线与辅助字段
    # ------------------------------------------------------------------
    def _iter_std_dates(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> list[datetime.date]:
        if isinstance(dates, datetime.date):
            return [dates]
        return list(dates)

    def _fetch_single_stock_range(
        self,
        ak: Any,
        asset: str,
        start: datetime.date,
        end: datetime.date,
    ) -> pd.DataFrame:
        """拉取某支股票在区间内的 Akshare 日线并标准化。"""
        pure_code = asset.split(".")[0]
        try:
            df = ak.stock_zh_a_daily(
                symbol=pure_code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            )
        except Exception:  # pragma: no cover - 单支股失败不应中断全盘
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(
            columns={
                "date": "date",
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
                "volume": "volume",
                "amount": "amount",
            }
        )
        keep = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
        df = df[keep]
        df["date"] = _to_std_date(df["date"])
        df["asset"] = asset
        return df

    def fetch_bars(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        dates = self._iter_std_dates(dates)
        if not dates:
            return _empty_bar_frame(), []
        ak = _require_akshare()
        if ak is None:
            return _empty_bar_frame(), [["fetch_bars", "import", "akshare 未安装"]]
        errors: ErrorRecord = []
        try:
            stock_list = self.fetch_stock_list()
        except Exception:  # pragma: no cover
            stock_list = None
        if stock_list is None or stock_list.empty:
            return _empty_bar_frame(), [["fetch_bars", "stock_list", "无法获取股票列表"]]
        assets = stock_list["asset"].tolist()
        start = min(dates)
        end = max(dates)
        frames: list[pd.DataFrame] = []
        for asset in assets:
            try:
                df = self._fetch_single_stock_range(ak, asset, start, end)
            except Exception as exc:
                errors.append(["fetch_bars", asset, str(exc)])
                continue
            if df is None or df.empty:
                continue
            df = df[df["date"].isin(pd.to_datetime(dates))]
            if not df.empty:
                frames.append(df)
        if not frames:
            return _empty_bar_frame(), errors
        out = pd.concat(frames, ignore_index=True)
        for col in STANDARD_BAR_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").astype("float64")
        out["amount"] = pd.to_numeric(out["amount"], errors="coerce").astype("float64")
        out["adjust"] = 1.0
        out["is_st"] = False
        out["up_limit"] = 0.0
        out["down_limit"] = 0.0
        out = out.sort_values(["date", "asset"]).reset_index(drop=True)
        return out[STANDARD_BAR_COLUMNS], errors

    def fetch_adjust_factor(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        errors: ErrorRecord = [
            ["fetch_adjust_factor", "all", "Akshare 暂不提供按日期批量的复权因子接口"]
        ]
        return (
            pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ms]"),
                    "asset": pd.Series(dtype="object"),
                    "adjust": pd.Series(dtype="float64"),
                }
            ),
            errors,
        )

    def fetch_limit_price(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        errors: ErrorRecord = [
            ["fetch_limit_price", "all", "Akshare 暂不提供按日期批量的涨跌停接口"]
        ]
        return (
            pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ms]"),
                    "asset": pd.Series(dtype="object"),
                    "up_limit": pd.Series(dtype="float64"),
                    "down_limit": pd.Series(dtype="float64"),
                }
            ),
            errors,
        )

    def fetch_st_info(
        self, dates: Iterable[datetime.date] | datetime.date
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        errors: ErrorRecord = [
            ["fetch_st_info", "all", "Akshare 暂不提供按日期批量的 ST 标记接口"]
        ]
        return (
            pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ms]"),
                    "asset": pd.Series(dtype="object"),
                    "is_st": pd.Series(dtype="boolean"),
                }
            ),
            errors,
        )

    def fetch_bars_ext(
        self,
        dates: Iterable[datetime.date] | datetime.date,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        if phase_callback:
            phase_callback("bars")
        bars, errors1 = self.fetch_bars(dates)

        if phase_callback:
            phase_callback("adjust")
        adjust, errors2 = self.fetch_adjust_factor(dates)

        if phase_callback:
            phase_callback("limit")
        limit, errors3 = self.fetch_limit_price(dates)

        if phase_callback:
            phase_callback("st")
        st, errors4 = self.fetch_st_info(dates)

        errors: ErrorRecord = []
        errors.extend(errors1)
        errors.extend(errors2)
        errors.extend(errors3)
        errors.extend(errors4)

        if bars is None or bars.empty:
            return _empty_bar_frame(), errors

        df = bars.merge(adjust, on=["date", "asset"], how="left")
        df = df.merge(st, on=["date", "asset"], how="left")
        df = df.merge(limit, on=["date", "asset"], how="left")

        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("float64")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").astype("float64")
        df["adjust"] = df["adjust"].fillna(1.0)
        df["is_st"] = df["is_st"].fillna(False)
        df["up_limit"] = df["up_limit"].fillna(0.0)
        df["down_limit"] = df["down_limit"].fillna(0.0)

        for col in STANDARD_BAR_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df.sort_values(["date", "asset"]).reset_index(drop=True)
        return df[STANDARD_BAR_COLUMNS], errors

    def fetch_daily_basic(
        self,
        dates: Iterable[datetime.date] | datetime.date,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        errors: ErrorRecord = [
            ["fetch_daily_basic", "all", "Akshare 暂不提供按日期批量的市值接口"]
        ]
        return (
            pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ms]"),
                    "asset": pd.Series(dtype="object"),
                    "total_mv": pd.Series(dtype="float64"),
                    "circ_mv": pd.Series(dtype="float64"),
                }
            ),
            errors,
        )

    # ------------------------------------------------------------------
    # 指数
    # ------------------------------------------------------------------
    def fetch_index_bars(
        self,
        index_codes: Iterable[str] | str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        ak = _require_akshare()
        if ak is None:
            return _empty_index_bar_frame(), [["fetch_index_bars", "import", "akshare 未安装"]]
        if isinstance(index_codes, str):
            index_codes = [index_codes]
        frames: list[pd.DataFrame] = []
        errors: ErrorRecord = []
        for code in index_codes:
            try:
                df = ak.stock_zh_index_daily(
                    symbol=_to_ak_index_code(code),
                    start_date=start_date.strftime("%Y%m%d"),
                    end_date=end_date.strftime("%Y%m%d"),
                )
            except Exception as exc:
                errors.append(["fetch_index_bars", code, str(exc)])
                continue
            if df is None or df.empty:
                continue
            df = df.rename(columns={"date": "date"})
            df["date"] = _to_std_date(df["date"])
            df["index_code"] = code
            frames.append(df)
        if not frames:
            return _empty_index_bar_frame(), errors
        out = pd.concat(frames, ignore_index=True)
        keep = [c for c in STANDARD_INDEX_BAR_COLUMNS if c in out.columns]
        out = out[keep]
        for col in STANDARD_INDEX_BAR_COLUMNS:
            if col not in out.columns:
                out[col] = pd.NA
        out = out.sort_values(["date", "index_code"]).reset_index(drop=True)
        return out[STANDARD_INDEX_BAR_COLUMNS], errors

    def fetch_index_weights(
        self,
        index_codes: Iterable[str] | str,
        snapshot_dates: Iterable[datetime.date],
    ) -> tuple[pd.DataFrame, ErrorRecord]:
        errors: ErrorRecord = [
            [
                "fetch_index_weights",
                "all",
                "Akshare 暂不提供指数成分历史 PIT 接口，请使用 Tushare 取成分",
            ]
        ]
        return _empty_index_weight_frame(), errors

