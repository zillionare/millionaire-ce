"""Coverage contracts for date-based historical source responses."""

import datetime

import pandas as pd
import polars as pl


def trading_sessions(calendar, start, end):
    """Request only sessions within the source interval, excluding calendar floor carry-in."""
    from quantide.core.enums import FrameType

    return [day for day in calendar.get_frames(start, end, FrameType.DAY) if start <= day <= end]


def require_date_coverage(frame, dates, dataset: str) -> None:
    """Reject empty or partial responses before recording a successful checkpoint."""
    expected = {day.date() if isinstance(day, datetime.datetime) else day for day in dates}
    if not expected:
        return
    if frame is None or len(frame) == 0:
        raise RuntimeError(f"{dataset} returned no coverage for requested sessions")
    values = frame["date"].to_list() if isinstance(frame, pl.DataFrame) else list(frame["date"])
    actual = set(pd.to_datetime(values, errors="raise").date)
    missing = sorted(expected - actual)
    if missing:
        raise RuntimeError(f"{dataset} missing requested sessions: {missing[:5]}")
