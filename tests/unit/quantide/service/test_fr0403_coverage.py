"""FR-0403 coverage tests for mock-only bar feed and live quote boundaries."""

import datetime

import polars as pl
import pytest

from quantide.service.datafeed import BarsFeedImpl
from quantide.service.market_data_hub import MarketDataHub


class _HistoryPort:
    """FR-0403 history port double returning a fixed daily bar."""

    def get_bars_in_range(self, **_kwargs):
        return pl.DataFrame(
            {
                "date": [datetime.datetime(2024, 1, 2)],
                "asset": ["000001.SZ"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [100],
            }
        )


@pytest.fixture
def quote() -> MarketDataHub:
    """FR-0403: construct a fresh in-memory hub without a network worker."""
    instance = MarketDataHub()
    yield instance
    instance.stop()


def test_bars_feed_normalizes_history_port_response() -> None:
    """FR-0403 AC-1: history bars are normalized to the documented standard columns."""
    bars = BarsFeedImpl(daily_bars=_HistoryPort()).get_bars(
        "000001.SZ", datetime.date(2024, 1, 1), datetime.date(2024, 1, 2)
    )

    assert bars.columns == [
        "asset", "frame", "open", "high", "low", "close", "volume", "amount",
        "adjust", "is_st", "up_limit", "down_limit",
    ]
    assert bars.row(0, named=True)["close"] == 10.5
    assert bars.row(0, named=True)["is_st"] is False


def test_bars_feed_returns_typed_empty_frame_when_history_is_absent() -> None:
    """FR-0403 AC-2: a feed without history has an explicit empty frame result."""
    bars = BarsFeedImpl().get_bars("missing", datetime.date(2024, 1, 1))

    assert bars.is_empty()
    assert bars.schema["asset"] == pl.String


def test_market_data_hub_payload_updates_snapshot_and_malformed_input_preserves_cache(quote) -> None:
    """FR-0403 AC-3: valid payloads populate snapshots while malformed payloads do not pollute cache."""
    quote.subscribe(["000001.SZ"])
    quote._on_quotes({"000001.SZ": {"price": "10.5", "open": "10", "high": "11", "low": "9", "volume": 3}})
    before = quote.snapshot(["000001.SZ"])
    snapshot = quote.snapshot(["000001.SZ", "unknown"])

    assert snapshot["000001.SZ"].price == 10.5
    assert "unknown" not in snapshot
    quote._on_quotes({"000001.SZ": "not-a-snapshot"})
    assert quote.snapshot(["000001.SZ"]) == before


def test_market_data_hub_tracks_subscriptions(quote) -> None:
    """FR-0403 AC-4: subscriptions are local to the market-data hub."""
    quote.subscribe(["000001.SZ", "000002.SZ"])
    quote.unsubscribe(["000002.SZ"])

    assert quote._subscribed == {"000001.SZ"}
