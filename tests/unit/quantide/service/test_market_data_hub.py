"""行情发布/订阅中心与发布者的单元测试。"""

from __future__ import annotations

import datetime
import time

from quantide.core.enums import Topics
from quantide.core.message import msg_hub
from quantide.data.fetchers.tencent import from_tencent_symbol, to_tencent_symbol
from quantide.providers.tencent_publisher import TencentPublisher
from quantide.service.market_data_hub import (
    MarketDataHub,
    market_data_hub,
    parse_snapshot,
)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """等待消息总线异步分发完成。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_parse_snapshot_builds_quote_snapshot():
    """原始字典转 QuoteSnapshot。"""
    snapshot = parse_snapshot(
        "600519.SH",
        {
            "price": 1318.49,
            "open": 1303.21,
            "high": 1322.0,
            "low": 1296.0,
            "volume": 42491,
            "amount": 557622.0,
            "ts": "2026-10-22T15:00:00",
        },
    )
    assert snapshot is not None
    assert snapshot.symbol == "600519.SH"
    assert snapshot.price == 1318.49
    assert snapshot.high == 1322.0
    assert snapshot.prev_close is None
    assert snapshot.ts == datetime.datetime(2026, 10, 22, 15, 0)


def test_parse_snapshot_rejects_invalid_price():
    """无有效价格时返回 None。"""
    assert parse_snapshot("600519.SH", {"price": 0}) is None
    assert parse_snapshot("600519.SH", {"price": "abc"}) is None
    assert parse_snapshot("600519.SH", {}) is None


def test_hub_receives_published_snapshots():
    """发布者推送的快照进入订阅中心。"""
    hub = MarketDataHub()
    hub.start()
    try:
        msg_hub.publish(
            Topics.QUOTES_ALL.value,
            {
                "600519.SH": {
                    "price": 1318.49,
                    "open": 1303.21,
                    "high": 1322.0,
                    "low": 1296.0,
                    "volume": 42491,
                    "amount": 557622.0,
                    "ts": "2026-10-22T15:00:00",
                }
            },
        )
        assert _wait_until(lambda: "600519.SH" in hub.snapshot(["600519.SH"]))
        snapshot = hub.snapshot(["600519.SH"])
        assert "600519.SH" in snapshot
        assert snapshot["600519.SH"].price == 1318.49
        assert hub.last_price("600519.SH") == 1318.49
    finally:
        hub.stop()


def test_hub_filters_by_subscription():
    """未订阅的标的不进入缓存。"""
    hub = MarketDataHub()
    hub.start()
    try:
        hub.subscribe(["600519.SH"])
        msg_hub.publish(
            Topics.QUOTES_ALL.value,
            {
                "600519.SH": {"price": 100.0},
                "000001.SZ": {"price": 200.0},
            },
        )
        assert _wait_until(lambda: "600519.SH" in hub.snapshot(["600519.SH"]))
        assert "000001.SZ" not in hub.snapshot(["000001.SZ"])
    finally:
        hub.stop()


def test_hub_receives_price_limits():
    """涨跌停价推送进入订阅中心。"""
    hub = MarketDataHub()
    hub.start()
    try:
        msg_hub.publish(
            Topics.STOCK_LIMIT.value,
            {"600519.SH": {"up_limit": 1440.0, "down_limit": 1180.0}},
        )
        assert _wait_until(lambda: hub.get_limits("600519.SH") != (0.0, 0.0))
        assert hub.get_limits("600519.SH") == (1180.0, 1440.0)
    finally:
        hub.stop()


def test_hub_ignore_invalid_payload():
    """非法载荷被忽略。"""
    hub = MarketDataHub()
    hub.start()
    try:
        msg_hub.publish(Topics.QUOTES_ALL.value, {"600519.SH": {"price": 0}})
        msg_hub.publish(Topics.QUOTES_ALL.value, {"600519.SH": "not-a-dict"})
        assert hub.snapshot(["600519.SH"]) == {}
    finally:
        hub.stop()


def test_global_hub_is_singleton():
    """全局单例可用。"""
    assert isinstance(market_data_hub, MarketDataHub)


def test_tencent_symbol_conversion():
    """统一代码与腾讯代码互转。"""
    assert to_tencent_symbol("600519.SH") == "sh600519"
    assert to_tencent_symbol("000001.SZ") == "sz000001"
    assert to_tencent_symbol("399006.SZ") == "sz399006"
    assert from_tencent_symbol("sh600519") == "600519.SH"
    assert from_tencent_symbol("sz000001") == "000001.SZ"


class _FakeTencentFetcher:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch_quote_snapshot(self, symbols):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return dict(self.snapshot)


def test_tencent_publisher_publishes_quotes_and_limits():
    """发布实时快照，同时发布当日涨跌停。"""
    fake = _FakeTencentFetcher(
        {
            "600519.SH": {
                "price": 1318.49,
                "prev_close": 1300.0,
                "open": 1303.21,
                "high": 1322.0,
                "low": 1296.0,
                "volume": 42491,
                "amount": 557622.0,
                "up_limit": 1434.0,
                "down_limit": 1174.0,
                "ts": datetime.datetime(2026, 10, 22, 15, 0),
            }
        }
    )
    publisher = TencentPublisher(["600519.SH"], fetcher=fake)

    quotes: list[dict] = []
    limits: list[dict] = []
    msg_hub.subscribe(Topics.QUOTES_ALL.value, quotes.append)
    msg_hub.subscribe(Topics.STOCK_LIMIT.value, limits.append)
    try:
        assert publisher.publish_once() == 1
        assert _wait_until(lambda: bool(quotes) and bool(limits))
    finally:
        msg_hub.unsubscribe(Topics.QUOTES_ALL.value, quotes.append)
        msg_hub.unsubscribe(Topics.STOCK_LIMIT.value, limits.append)

    assert quotes[0]["600519.SH"]["price"] == 1318.49
    assert quotes[0]["600519.SH"]["prev_close"] == 1300.0
    assert limits[0]["600519.SH"]["up"] == 1434.0
    assert limits[0]["600519.SH"]["up_limit"] == 1434.0


def test_tencent_publisher_survives_fetch_error():
    """拉取失败时发布 0 条而不抛异常。"""
    publisher = TencentPublisher(
        ["600519.SH"], fetcher=_FakeTencentFetcher(RuntimeError("网络错误"))
    )
    assert publisher.publish_once() == 0


def test_parse_snapshot_includes_prev_close():
    """除权日昨收价随快照进入订阅中心。"""
    snapshot = parse_snapshot(
        "600519.SH",
        {"price": 95.5, "prev_close": 95.0, "ts": "2024-01-03T10:00:00"},
    )
    assert snapshot is not None
    assert snapshot.prev_close == 95.0
