"""指数成分（PIT）存储的单元测试。"""

from __future__ import annotations

import datetime

import polars as pl
import pytest

from quantide.data.stores.index_weight import IndexWeightStore


@pytest.fixture
def store(asset_dir, tmp_path) -> IndexWeightStore:
    """构造成分存储并写入两期快照。"""
    from quantide.data.models.calendar import Calendar

    cal = Calendar()
    cal.load(asset_dir / "baseline_calendar.parquet")
    st = IndexWeightStore(tmp_path / "index_weight", cal)
    frame = pl.DataFrame(
        {
            "date": [
                datetime.date(2024, 1, 31),
                datetime.date(2024, 1, 31),
                datetime.date(2024, 6, 28),
                datetime.date(2024, 6, 28),
            ],
            "index_code": ["000300.SH", "000300.SH", "000300.SH", "000300.SH"],
            "con_code": ["600519.SH", "000001.SZ", "600519.SH", "300750.SZ"],
            "weight": [1.0, 0.5, 1.1, 0.6],
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    st.append_data(frame)
    return st


def test_constituents_picks_latest_snapshot_at_or_before_as_of(store):
    """PIT 查询取 as_of 之前最近的一期快照。"""
    frame = store.constituents("000300.SH", datetime.date(2024, 7, 1))
    assert frame.height == 2
    assert set(frame["con_code"].to_list()) == {"600519.SH", "300750.SZ"}
    assert frame["date"][0] == datetime.date(2024, 6, 28)


def test_constituents_before_first_snapshot_is_empty(store):
    """早于首期快照时返回空。"""
    frame = store.constituents("000300.SH", datetime.date(2023, 12, 31))
    assert frame.is_empty()


def test_constituents_unknown_index_is_empty(store):
    """未知指数返回空。"""
    assert store.constituents("999999.SH", datetime.date(2024, 7, 1)).is_empty()


def test_snapshot_dates_lists_all(store):
    """快照日列表。"""
    dates = store.snapshot_dates("000300.SH")
    assert dates == [datetime.date(2024, 1, 31), datetime.date(2024, 6, 28)]


def test_coverage_reports_range_and_count(store):
    """覆盖统计包含起止日与期数。"""
    coverage = store.coverage()
    assert "000300.SH" in coverage
    entry = coverage["000300.SH"]
    assert entry["snapshots"] == 2
    assert entry["start"] == datetime.date(2024, 1, 31)
    assert entry["name"] == "沪深300"


def test_deduplicate_by_business_key(store):
    """同键重复写入按主键去重（保留最新）。"""
    again = pl.DataFrame(
        {
            "date": [datetime.date(2024, 6, 28)],
            "index_code": ["000300.SH"],
            "con_code": ["600519.SH"],
            "weight": [9.9],
        }
    ).with_columns(pl.col("date").cast(pl.Date))
    store.append_data(again)
    frame = store.constituents("000300.SH", datetime.date(2024, 7, 1))
    assert frame.height == 2
    row = frame.filter(pl.col("con_code") == "600519.SH").row(0, named=True)
    assert row["weight"] == 9.9
