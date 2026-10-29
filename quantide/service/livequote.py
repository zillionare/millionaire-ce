"""Compatibility names for the retired live-quote implementation.

Real-time data has one implementation: :mod:`market_data_hub`.  This module
contains no publisher, websocket client, cache, or subscriber of its own.  It
exists temporarily for integrations that still import the old names and
returns the MarketDataHub API directly.
"""

from quantide.service.market_data_hub import MarketDataHub, market_data_hub

LiveQuote = MarketDataHub
live_quote = market_data_hub

__all__ = ["LiveQuote", "live_quote"]
