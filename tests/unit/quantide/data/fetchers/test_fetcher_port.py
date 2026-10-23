"""数据源端口契约与多源切换的单元测试。"""

from __future__ import annotations

import datetime

import pandas as pd

from quantide.core.ports import DataFetcherPort
from quantide.core.ports.data_fetcher import (
    STANDARD_BAR_COLUMNS,
    STANDARD_INDEX_BAR_COLUMNS,
    STANDARD_INDEX_WEIGHT_COLUMNS,
    STANDARD_STOCK_LIST_COLUMNS,
)
from quantide.data.fetchers.akshare import AkshareDataFetcher
from quantide.data.fetchers.registry import get_data_fetcher
from quantide.data.fetchers.tushare import TushareDataFetcher


def test_data_fetcher_port_has_explicit_column_contracts():
    """端口契约暴露标准化字段集。"""
    from quantide.core.ports.data_fetcher import (
        STANDARD_BAR_COLUMNS,
        STANDARD_INDEX_WEIGHT_COLUMNS,
    )

    assert "date" in STANDARD_BAR_COLUMNS and "asset" in STANDARD_BAR_COLUMNS
    assert "date" in STANDARD_INDEX_BAR_COLUMNS and "index_code" in STANDARD_INDEX_BAR_COLUMNS
    assert "con_code" in STANDARD_INDEX_WEIGHT_COLUMNS
    assert STANDARD_STOCK_LIST_COLUMNS[0] == "asset"


def test_port_protocol_methods_exist():
    """协议声明全部方法。"""
    expected = [
        "fetch_calendar",
        "fetch_stock_list",
        "fetch_adjust_factor",
        "fetch_bars",
        "fetch_limit_price",
        "fetch_st_info",
        "fetch_bars_ext",
        "fetch_daily_basic",
        "fetch_index_bars",
        "fetch_index_weights",
    ]
    for name in expected:
        assert hasattr(DataFetcherPort, name)


def test_tushare_fetcher_implements_port():
    """Tushare 适配器实现标准端口。"""
    fetcher = TushareDataFetcher()
    assert fetcher.source_name == "tushare"
    for name in [
        "fetch_calendar",
        "fetch_stock_list",
        "fetch_bars_ext",
        "fetch_index_bars",
        "fetch_index_weights",
    ]:
        assert callable(getattr(fetcher, name))


def test_akshare_fetcher_registered_and_switchable():
    """注册表可按名称取到 akshare 适配器；默认仍为 tushare。"""
    ak = get_data_fetcher("akshare")
    assert isinstance(ak, AkshareDataFetcher)
    assert ak.source_name == "akshare"

    default_fetcher = get_data_fetcher()
    assert isinstance(default_fetcher, TushareDataFetcher)


def test_akshare_returns_empty_frames_without_network(monkeypatch):
    """未安装/不可联网时，Akshare 方法返回空标准框架并记录错误。"""

    def _none():
        return None

    monkeypatch.setattr(
        "quantide.data.fetchers.akshare._require_akshare", _none
    )
    fetcher = AkshareDataFetcher()

    d = datetime.date(2024, 1, 2)

    df, errs = fetcher.fetch_bars(d)
    assert isinstance(df, pd.DataFrame)
    assert df.empty
    assert list(df.columns) == STANDARD_BAR_COLUMNS
    assert errs

    df, errs = fetcher.fetch_index_weights(["000300.SH"], [d])
    assert df.empty
    assert list(df.columns) == STANDARD_INDEX_WEIGHT_COLUMNS

    df, errs = fetcher.fetch_index_bars(["000300.SH"], d, d)
    assert df.empty
    assert errs

    assert fetcher.fetch_stock_list() is None
    cal = fetcher.fetch_calendar(d)
    assert cal.empty
