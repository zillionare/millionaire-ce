"""Crash-safe state publication and recovery of derived date indexes."""

import datetime
import json

import polars as pl
import pytest

from quantide.core.atomic_files import atomic_path, write_json, write_parquet
from quantide.data.stores.base import ParquetStorage


def test_failed_publish_keeps_committed_file_and_removes_temporary(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"running": True})
    with pytest.raises(RuntimeError):
        with atomic_path(path) as temporary:
            temporary.write_text("incomplete", encoding="utf-8")
            raise RuntimeError("shutdown before publication")
    assert json.loads(path.read_text()) == {"running": True}
    assert not list(tmp_path.glob(".state.json.*"))


def test_json_keeps_previous_generation_without_replacing_good_backup_with_corruption(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"generation": 1})
    write_json(path, {"generation": 2})
    backup = path.with_name("state.json.bak")
    assert json.loads(backup.read_text()) == {"generation": 1}
    path.write_text("truncated", encoding="utf-8")
    with pytest.raises(ValueError):
        write_json(path, {"generation": 3})
    assert path.read_text() == "truncated"
    assert json.loads(backup.read_text()) == {"generation": 1}


def test_date_index_rebuilds_after_data_commit_without_index_commit(tmp_path):
    path = tmp_path / "bars.parquet"
    first = datetime.date(2024, 1, 2)
    second = datetime.date(2024, 1, 3)
    store = ParquetStorage("test", path, calendar=None)
    store.append_data(pl.DataFrame({"date": [first], "asset": ["000001.SZ"]}))
    store._collect_dates()  # create a matching index fingerprint
    # Simulate a process dying after the durable data replacement and before index publication.
    write_parquet(pl.DataFrame({"date": [first, second], "asset": ["000001.SZ"] * 2}), path)
    restored = ParquetStorage("test", path, calendar=None)
    assert restored.end == second
    assert restored._dates.to_list() == [first, second]
    restored._dates_file_path().write_bytes(b"damaged derived cache")
    restored._collect_dates()
    assert restored._dates.to_list() == [first, second]
    # A damaged fingerprint is also derived state and can be safely rebuilt.
    restored._dates_file_path().with_suffix(".signature.json").write_text("broken")
    restored._collect_dates()
    assert restored.end == second
