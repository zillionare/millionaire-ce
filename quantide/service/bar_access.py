"""Shared compatibility boundary for historical bars and broker bar-view facades."""

import datetime

import polars as pl

from quantide.core.bar_schema import bar_schema


def previous_session(calendar, today):
    """Use the exchange calendar, retaining the documented fallback for tiny test calendars."""
    try:
        return calendar.day_shift(today, -1)
    except Exception:
        return today - datetime.timedelta(days=1)


def history_end(end_dt, today, calendar):
    """Keep completed-bar queries before the session open on the previous session."""
    end = end_dt.date() if isinstance(end_dt, datetime.datetime) else end_dt or today
    if isinstance(end_dt, datetime.datetime) and end_dt.time() <= datetime.time(9, 30):
        return previous_session(calendar, end)
    return end


def empty_history():
    """Return a fresh, typed empty historical daily-bar frame."""
    return pl.DataFrame(schema=bar_schema())


def history_source(primary, fallback, fallback_ready=True):
    """Select an available history interface without issuing a request."""
    if primary is not None and (hasattr(primary, "get_history") or hasattr(primary, "get_bars")):
        return primary
    return fallback if fallback_ready else None


def provider_history(provider, asset, count, end_date, frame_type, *, eager=None):
    """Retain each caller's eager/adjustment semantics at a single provider dispatch."""
    if provider is None:
        return None
    if hasattr(provider, "get_history"):
        return provider.get_history(asset, count, end_date, frame_type)
    if not hasattr(provider, "get_bars"):
        return None
    options = {"n": count, "end": end_date, "assets": [asset], "adjust": None}
    if eager is not None:
        options["eager_mode"] = eager
    return provider.get_bars(**options)


class BarViewFacade:
    """Expose range reads through the broker's configured historical/live view."""

    def get_bars_range(
        self, assets, start_dt, end_dt=None, adjust=None, include_forming_bar=True
    ) -> pl.DataFrame:
        """Delegate a range read to the broker-owned bar view."""
        return self._bar_view.get_bars_range(
            assets, start_dt, end_dt, adjust=adjust, include_forming_bar=include_forming_bar
        )
