"""Find internal minute-history gaps using stored bars and explicit suspension evidence."""

import datetime
import json
from pathlib import Path

import polars as pl
from filelock import FileLock

from quantide.core.atomic_files import write_json
from quantide.data.coverage import trading_sessions
from quantide.service.minute_backfill import _asset_lifespans, asset_interval


def _evidence_path(store):
    return Path(str(store._store_path) + ".suspensions.json")


def suspension_evidence(store):
    path = _evidence_path(store)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def record_suspensions(store, asset, start, end, inactive):
    """Persist only source-confirmed absences, replacing the checked interval."""
    path = _evidence_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".transaction.lock", timeout=30):
        evidence = suspension_evidence(store)
        previous = {
            day
            for day in evidence.get(asset, [])
            if not start.isoformat() <= day <= end.isoformat()
        }
        evidence[asset] = sorted(previous | {day.isoformat() for day in inactive})
        write_json(path, evidence)


def _complete_sessions(store, start, end, period):
    frame = store.get(start=start, end=end, eager_mode=False)
    if "date" not in frame.collect_schema().names():
        return {}
    times = [
        (datetime.datetime.combine(start, opening) + datetime.timedelta(minutes=offset)).time()
        for opening in (datetime.time(9, 30), datetime.time(13))
        for offset in range(period, 121, period)
    ]
    valid = pl.col("date").dt.time().is_in(times) & pl.all_horizontal(
        [pl.col(key).is_finite() & (pl.col(key) > 0) for key in ("open", "high", "low", "close")]
    )
    complete = (
        frame.group_by("asset", pl.col("date").cast(pl.Date).alias("day"))
        .agg(pl.col("date").n_unique().alias("count"), valid.all().alias("valid"))
        .filter((pl.col("count") == len(times)) & pl.col("valid"))
        .collect()
    )
    result = {}
    for asset, day in complete.select("asset", "day").iter_rows():
        result.setdefault(asset, set()).add(day)
    return result


def coverage_starts(store, assets, start, end, period):
    """Return the first missing session per asset, or None for complete history."""
    from quantide.data.models.calendar import calendar

    complete = _complete_sessions(store, start, end, int(period))
    evidence, lifespans = suspension_evidence(store), _asset_lifespans()
    result = {}
    for asset in assets:
        first, last = asset_interval(lifespans.get(asset, {}), start, end)
        if first > last:
            result[asset] = None
            continue
        inactive = {datetime.date.fromisoformat(day) for day in evidence.get(asset, [])}
        result[asset] = _first_missing(calendar, first, last, complete.get(asset, set()) | inactive)
    return result


def _first_missing(calendar, start, end, covered):
    missing = set(trading_sessions(calendar, start, end)) - covered
    return min(missing) if missing else None
