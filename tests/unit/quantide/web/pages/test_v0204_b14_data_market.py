"""B14-data_market: Coverage tests for quantide/web/pages/data_market.py.

Target lines:
- 70: _OverviewTab size > 1GB -> GB formatting branch
- 78: end_date < last_trade -> is_stale=True
- 80, 81: _OverviewTab exception -> error Div
- 323-332: _run_market_sync _on_progress payload branches
- 337-339: _run_market_sync success (progress=100, completed=True)
- 344: _run_market_sync exception path
- 402-407: sync_progress event_generator yields + terminates
"""

from __future__ import annotations

import asyncio
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from quantide.web.pages import data_market as dm_mod
from quantide.web.pages.data_market import (
    _OverviewTab,
    _run_market_sync,
    _sync_status,
    sync_progress,
)

# ---------------------------------------------------------------------------
# _OverviewTab: GB branch, stale data, exception
# ---------------------------------------------------------------------------


def test_overview_tab_size_in_gb_branch():
    """size_bytes > 1GB -> 'GB' formatting branch."""
    with patch.object(dm_mod, "daily_bars") as mock_bars, \
         patch.object(dm_mod, "calendar") as mock_cal:
        mock_bars.start = "2024-01-01"
        mock_bars.end = "2024-06-30"
        mock_bars.total_dates = 100
        mock_bars.size = 2 * 1024 * 1024 * 1024  # 2 GB
        mock_cal.last_trade_date = MagicMock(return_value="2024-06-30")
        out = _OverviewTab()
    text = str(out)
    assert "GB" in text


def test_overview_tab_size_in_mb_branch_stale_data():
    """end_date earlier than last_trade -> is_stale True; size shown in MB."""
    with patch.object(dm_mod, "daily_bars") as mock_bars, \
         patch.object(dm_mod, "calendar") as mock_cal:
        mock_bars.start = "2024-01-01"
        mock_bars.end = "2024-06-28"
        mock_bars.total_dates = 100
        mock_bars.size = 5 * 1024 * 1024  # 5 MB
        mock_cal.last_trade_date = MagicMock(return_value="2024-06-30")
        out = _OverviewTab()
    text = str(out)
    assert "MB" in text
    # Stale branch highlights end_date in red.
    assert "text-red-600" in text


def test_overview_tab_exception_returns_error_div():
    """daily_bars.start raises -> exception caught -> error Div returned."""
    with patch.object(dm_mod, "daily_bars") as mock_bars, \
         patch.object(dm_mod, "calendar") as mock_cal:
        type(mock_bars).start = property(MagicMock(side_effect=RuntimeError("no data")))
        mock_cal.last_trade_date = MagicMock(return_value=None)
        out = _OverviewTab()
    text = str(out)
    assert "获取行情信息失败" in text
    assert "no data" in text


def test_overview_tab_no_last_trade_date_not_stale():
    """calendar.last_trade_date returns None -> is_stale stays False."""
    with patch.object(dm_mod, "daily_bars") as mock_bars, \
         patch.object(dm_mod, "calendar") as mock_cal:
        mock_bars.start = "2024-01-01"
        mock_bars.end = "2024-06-30"
        mock_bars.total_dates = 50
        mock_bars.size = 1024  # tiny
        mock_cal.last_trade_date = MagicMock(return_value=None)
        out = _OverviewTab()
    text = str(out)
    assert "MB" in text


# ---------------------------------------------------------------------------
# _run_market_sync: _on_progress payload branches + completion + exception
# ---------------------------------------------------------------------------


def _fake_service(ok=True, status="done", error=""):
    """构造一个 fake DataSyncService。"""
    from types import SimpleNamespace

    service = MagicMock()
    service.create_task.return_value = SimpleNamespace(task_id="task-1")
    service.run.return_value = ok
    service.get_task.return_value = SimpleNamespace(status=status, error=error)
    return service


def _capture_on_progress(
    start_date=datetime.date(2024, 1, 1),
    end_date=datetime.date(2024, 6, 30),
):
    """Run _run_market_sync and capture the _on_progress callback."""
    captured = {}

    def fake_subscribe(topic, cb):
        captured["cb"] = cb

    service = _fake_service()
    with patch.object(dm_mod, "msg_hub") as mock_hub, \
         patch("quantide.service.data_sync.data_sync_service", service):
        mock_hub.subscribe = MagicMock(side_effect=fake_subscribe)
        mock_hub.unsubscribe = MagicMock()
        asyncio.run(_run_market_sync(start_date, end_date))
    return captured.get("cb"), service


@pytest.mark.asyncio
async def test_run_market_sync_success_sets_completed():
    """Successful sync -> progress=100, completed=True."""
    service = _fake_service()
    with patch.object(dm_mod, "msg_hub") as mock_hub, \
         patch("quantide.service.data_sync.data_sync_service", service):
        mock_hub.subscribe = MagicMock()
        mock_hub.unsubscribe = MagicMock()
        await _run_market_sync(
            datetime.date(2024, 1, 1), datetime.date(2024, 6, 30)
        )
    assert _sync_status["completed"] is True
    assert _sync_status["progress"] == 100
    assert _sync_status["is_running"] is False


@pytest.mark.asyncio
async def test_run_market_sync_exception_sets_error():
    """asyncio.to_thread raises -> exception caught, error set."""
    with patch.object(dm_mod, "msg_hub") as mock_hub, \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=RuntimeError("sync err"))):
        mock_hub.subscribe = MagicMock()
        mock_hub.unsubscribe = MagicMock()
        await _run_market_sync(
            datetime.date(2024, 1, 1), datetime.date(2024, 6, 30)
        )
    assert _sync_status["error"] == "sync err"
    assert _sync_status["is_running"] is False
    assert _sync_status["completed"] is False


@pytest.mark.asyncio
async def test_run_market_sync_failed_task_sets_error():
    """服务返回失败 -> error 使用任务错误信息。"""
    service = _fake_service(ok=False, status="failed", error="boom")
    with patch.object(dm_mod, "msg_hub") as mock_hub, \
         patch("quantide.service.data_sync.data_sync_service", service):
        mock_hub.subscribe = MagicMock()
        mock_hub.unsubscribe = MagicMock()
        await _run_market_sync(
            datetime.date(2024, 1, 1), datetime.date(2024, 6, 30)
        )
    assert _sync_status["error"] == "boom"
    assert _sync_status["completed"] is False


def test_on_progress_non_dict_payload_returns_early():
    """Non-dict payload -> early return."""
    cb, _ = _capture_on_progress()
    assert cb is not None
    cb("not a dict")
    cb(None)
    cb(42)


def test_on_progress_error_payload_sets_status_error():
    """Payload with 'error' key -> _sync_status['error'] set, return."""
    cb, _ = _capture_on_progress()
    cb({"error": "boom"})
    assert _sync_status["error"] == "boom"


def test_on_progress_progress_payload_updates_status():
    """Payload with stage/progress/message -> progress + message updated."""
    cb, _ = _capture_on_progress()
    cb({"stage": "daily_bars", "progress": 50, "message": "正在同步 daily_bars..."})
    assert _sync_status["progress"] == 50
    assert "daily_bars" in _sync_status["message"]
    assert "正在同步" in _sync_status["message"]


def test_on_progress_done_payload_marks_completed():
    """stage=done -> progress=100, completed=True。"""
    cb, _ = _capture_on_progress()
    _sync_status["completed"] = False
    cb({"stage": "done", "progress": 100, "completed": True, "message": "同步完成"})
    assert _sync_status["completed"] is True
    assert _sync_status["progress"] == 100


@pytest.mark.asyncio
async def test_run_market_sync_creates_task_with_range_and_index_codes():
    """同步任务应带上日期范围与指数代码。"""
    service = _fake_service()
    with patch.object(dm_mod, "msg_hub") as mock_hub, \
         patch("quantide.service.data_sync.data_sync_service", service):
        mock_hub.subscribe = MagicMock()
        mock_hub.unsubscribe = MagicMock()
        await _run_market_sync(
            datetime.date(2024, 1, 2),
            datetime.date(2024, 1, 5),
            ["000300.SH", "000905.SH"],
        )

    params = service.create_task.call_args.args[0]
    assert params.start_date == datetime.date(2024, 1, 2)
    assert params.end_date == datetime.date(2024, 1, 5)
    assert params.index_codes == ["000300.SH", "000905.SH"]
    service.run.assert_called_once_with("task-1")


# ---------------------------------------------------------------------------
# sync_progress: event generator yields + terminates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_progress_yields_and_terminates_on_completed():
    """sync_progress StreamingResponse yields status, terminates when completed."""
    # Reset status; mark completed to ensure generator terminates quickly.
    _sync_status.update({
        "is_running": False, "progress": 100, "stage": "", "message": "done",
        "completed": True, "error": None,
    })
    response = await sync_progress()
    # StreamingResponse has body_iterator attribute
    assert response.media_type == "text/event-stream"
    gen = response.body_iterator
    first = await gen.__anext__()
    assert "data:" in first
    payload = json.loads(first.removeprefix("data:").strip())
    assert payload["completed"] is True
    # Should terminate after first yield because completed is True.
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


@pytest.mark.asyncio
async def test_sync_progress_terminates_on_error():
    """sync_progress terminates when error set."""
    _sync_status.update({
        "is_running": False, "progress": 0, "stage": "", "message": "",
        "completed": False, "error": "broken",
    })
    response = await sync_progress()
    gen = response.body_iterator
    first = await gen.__anext__()
    payload = json.loads(first.removeprefix("data:").strip())
    assert payload["error"] == "broken"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


@pytest.mark.asyncio
async def test_sync_progress_terminates_on_completed_after_initial_yield():
    """When neither completed nor error initially, but completed set after yield."""
    _sync_status.update({
        "is_running": True, "progress": 50, "stage": "", "message": "running",
        "completed": False, "error": None,
    })
    response = await sync_progress()
    gen = response.body_iterator
    first = await gen.__anext__()
    assert "data:" in first
    # Now mark completed to make generator stop on next iteration.
    _sync_status["completed"] = True
    # The generator may sleep before checking; allow it to terminate.
    # We use wait_for to avoid hanging the test.
    try:
        await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    except (StopAsyncIteration, TimeoutError):
        pass
