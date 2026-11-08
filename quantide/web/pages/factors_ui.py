"""Shared factor page labels and number formatting."""

from __future__ import annotations

from typing import Any

from quantide.core.market_values import datetime_or_none

LOOKBACK_LABELS = {"1y": "1 年", "3y": "3 年", "5y": "5 年", "all": "全部"}
PRICE_LABELS = {"open": "开盘价", "close": "收盘价"}


def fmt_number(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_percent(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_time(value: Any) -> str:
    parsed = datetime_or_none(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else "—"
