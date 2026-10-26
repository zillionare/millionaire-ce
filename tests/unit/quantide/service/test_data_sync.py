"""数据同步服务的单元测试。"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from quantide.service.data_sync import (
    DataSyncService,
    SyncParams,
    _month_end_snapshots,
)


def test_month_ranges_split_by_calendar_month():
    """区间按自然月切分。"""
    ranges = DataSyncService._month_ranges(
        datetime.date(2024, 1, 15), datetime.date(2024, 3, 10)
    )
    assert ranges == [
        (datetime.date(2024, 1, 15), datetime.date(2024, 1, 31)),
        (datetime.date(2024, 2, 1), datetime.date(2024, 2, 29)),
        (datetime.date(2024, 3, 1), datetime.date(2024, 3, 10)),
    ]


def test_sync_params_round_trip():
    """同步参数可序列化还原。"""
    params = SyncParams(
        start_date=datetime.date(2015, 1, 1),
        end_date=datetime.date(2026, 10, 19),
        index_codes=["000300.SH", "000905.SH"],
    )
    restored = SyncParams.from_dict(params.to_dict())
    assert restored.start_date == params.start_date
    assert restored.end_date == params.end_date
    assert restored.index_codes == params.index_codes


def test_task_lifecycle_is_persisted(db):
    """任务创建、查询与状态更新都落库。"""
    service = DataSyncService()
    params = SyncParams(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
    )
    task = service.create_task(params)
    assert task.task_id

    fetched = service.get_task(task.task_id)
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.to_params()["start_date"] == "2024-01-01"

    service._update_task(task.task_id, status="running", progress=50, message="x")
    again = service.get_task(task.task_id)
    assert again.status == "running"
    assert again.progress == 50

    listed = service.list_tasks()
    assert any(t.task_id == task.task_id for t in listed)


def test_run_records_stages_and_completion(db):
    """执行成功后记录已完成阶段。"""
    service = DataSyncService()
    params = SyncParams(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 1, 31)
    )
    task = service.create_task(params)

    with patch.object(DataSyncService, "_run_stage") as mock_stage:
        assert service.run(task.task_id) is True
    assert mock_stage.call_count == 7  # 七个阶段各一次

    done = service.get_task(task.task_id)
    assert done.status == "done"
    assert done.progress == 100
    assert len(done.stage_list()) == 7


def test_run_marks_failed_on_stage_error(db):
    """阶段异常时任务标记为失败并记录错误。"""
    service = DataSyncService()
    params = SyncParams(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 1, 31)
    )
    task = service.create_task(params)

    with patch.object(
        DataSyncService, "_run_stage", side_effect=RuntimeError("抓取失败")
    ):
        assert service.run(task.task_id) is False

    failed = service.get_task(task.task_id)
    assert failed.status == "failed"
    assert "抓取失败" in failed.error


def test_run_resumes_skipping_completed_stages(db):
    """重跑时跳过已完成阶段（断点续传）。"""
    service = DataSyncService()
    params = SyncParams(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 1, 31)
    )
    task = service.create_task(params)
    service._update_task(task.task_id, stages=["calendar", "stock_list"])

    with patch.object(DataSyncService, "_run_stage") as mock_stage:
        assert service.run(task.task_id) is True
    assert mock_stage.call_count == 5


def test_run_is_exclusive(db):
    """同一时间只允许一个任务执行。"""
    service = DataSyncService()
    params = SyncParams(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 1, 31)
    )
    task = service.create_task(params)

    with patch.object(DataSyncService, "_run_stage", side_effect=RuntimeError("x")):
        service.run(task.task_id)
    # 失败后锁应已释放
    assert service.is_running is False


def test_cancel_stops_between_stages(db):
    """取消请求会让任务在阶段之间停止。"""
    service = DataSyncService()
    params = SyncParams(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 1, 31)
    )
    task = service.create_task(params)

    def _cancel_after_first(*args, **kwargs):
        service._cancel.set()

    with patch.object(DataSyncService, "_run_stage", side_effect=_cancel_after_first):
        assert service.run(task.task_id) is False

    cancelled = service.get_task(task.task_id)
    assert cancelled.status == "cancelled"


def test_month_end_snapshots_uses_calendar(asset_dir):
    """成分快照日来自交易日历的月末。"""
    from quantide.data.models.calendar import calendar

    calendar.load(asset_dir / "baseline_calendar.parquet")
    snapshots = _month_end_snapshots(
        datetime.date(2024, 1, 1), datetime.date(2024, 3, 31)
    )
    assert snapshots
    assert all(isinstance(d, datetime.date) for d in snapshots)
    assert len(snapshots) == 3


def test_sync_calendar_updates_full_range(monkeypatch):
    """日历同步必须走完整 update，不能按本次起始日截断历史。"""
    from quantide.data.models.calendar import calendar

    calls: list[bool] = []
    monkeypatch.setattr(calendar, "update", lambda: calls.append(True))
    DataSyncService()._sync_calendar(
        SyncParams(
            start_date=datetime.date(2026, 10, 20),
            end_date=datetime.date(2026, 10, 23),
        )
    )
    assert calls == [True]
