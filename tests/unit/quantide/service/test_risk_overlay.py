"""Unit tests for the generic composite risk-overlay lifecycle."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from quantide.service.risk_overlay import CompositeRiskOverlay


class _RecordingOverlay:
    def __init__(self, name: str, calls: list[tuple[str, str, object]]) -> None:
        self.name = name
        self.calls = calls

    async def check_open(self, broker, tm):
        self.calls.append(("open", self.name, broker))

    async def check_intraday(self, broker, tm):
        self.calls.append(("intraday", self.name, broker))

    def sync_positions(self, positions, today):
        self.calls.append(("sync", self.name, (positions, today)))


def test_composite_preserves_selected_overlay_order() -> None:
    calls: list[tuple[str, str, object]] = []
    first = _RecordingOverlay("first", calls)
    second = _RecordingOverlay("second", calls)
    overlay = CompositeRiskOverlay([first, second], allow_same_bar_repurchase=False)
    broker = object()

    assert overlay.allow_same_bar_repurchase is False
    assert [item.name for item in overlay.overlays] == ["first", "second"]

    asyncio.run(overlay.check_open(broker, datetime.datetime(2024, 6, 4, 9, 30)))
    asyncio.run(overlay.check_intraday(broker, datetime.datetime(2024, 6, 4, 14, 55)))
    overlay.sync_positions({}, datetime.date(2024, 6, 4))

    assert calls == [
        ("open", "first", broker),
        ("open", "second", broker),
        ("intraday", "first", broker),
        ("intraday", "second", broker),
        ("sync", "first", ({}, datetime.date(2024, 6, 4))),
        ("sync", "second", ({}, datetime.date(2024, 6, 4))),
    ]


@pytest.mark.asyncio
async def test_composite_allows_same_bar_repurchase_by_default() -> None:
    overlay = CompositeRiskOverlay([])

    assert overlay.allow_same_bar_repurchase is True
    await overlay.check_open(object(), datetime.datetime(2024, 6, 4, 9, 30))
    await overlay.check_intraday(object(), datetime.datetime(2024, 6, 4, 14, 55))
    overlay.sync_positions({}, datetime.date(2024, 6, 4))
