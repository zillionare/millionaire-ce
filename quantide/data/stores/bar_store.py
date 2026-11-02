"""Shared partition policy and range reads for raw minute and index bars."""

import datetime
from pathlib import Path

import polars as pl

from quantide.data.stores.base import ParquetStorage, _as_datetime_bound


class BarStore(ParquetStorage):
    """Configure a raw-bar store with its explicit security identity column."""

    def __init__(self, name, path, calendar, identity):
        path = Path(path).expanduser()
        partition = None if path.suffix == ".parquet" else "year"
        super().__init__(name, path, calendar, None, error_handler=None, partition_by=partition)
        self._id_cols = ["date", identity]

    def _read_bars(self, identifiers, start, end, eager, *, full_day=False):
        frame = self._scan_store(keep_partition_col=False)
        if frame is None:
            return pl.DataFrame() if eager else pl.LazyFrame()
        if start is not None:
            frame = frame.filter(pl.col("date") >= _as_datetime_bound(start))
        if end is not None:
            if full_day:
                bound = pl.lit(
                    end
                    if isinstance(end, datetime.datetime)
                    else datetime.datetime.combine(end, datetime.time.max)
                )
            else:
                bound = _as_datetime_bound(end)
            frame = frame.filter(pl.col("date") <= bound)
        if identifiers is not None:
            frame = frame.filter(pl.col(self._id_cols[1]).is_in(identifiers))
        return frame.collect() if eager else frame
