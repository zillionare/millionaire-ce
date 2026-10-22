"""行情发布/订阅中心与发布者的单元测试。"""

from __future__ import annotations

import datetime
import time

from quantide.core.enums import Topics
from quantide.core.message import msg_hub
from quantide.providers.akshare_publisher import (
    DEFAULT_SYMBOLS,
    AksharePublisher,
    _akshare_to_unified,
    _to_market_symbol,
)
from quantide.providers.pytdx_publisher import PytdxPublisher, _split_symbol
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


def test_akshare_publisher_defaults_to_50_symbols():
    """示例发布者默认 50 支标的。"""
    assert len(DEFAULT_SYMBOLS) == 50
    publisher = AksharePublisher()
    assert len(publisher.symbols) == 50


def test_akshare_symbol_conversion():
    """代码格式转换。"""
    assert _to_market_symbol("600519") == "600519.SH"
    assert _to_market_symbol("000001") == "000001.SZ"
    assert _akshare_to_unified("sh600519") == "600519.SH"
    assert _akshare_to_unified("sz000001") == "000001.SZ"


def test_akshare_publisher_publishes_snapshot(monkeypatch):
    """发布者把拉到的快照发布到总线。"""
    publisher = AksharePublisher(["600519.SH"])

    def _fake_fetch():
        return {
            "600519.SH": {
                "price": 1318.49,
                "open": 1303.21,
                "high": 1322.0,
                "low": 1296.0,
                "volume": 42491,
                "amount": 557622.0,
                "ts": "2026-10-22T15:00:00",
            }
        }

    monkeypatch.setattr(publisher, "_fetch_snapshot", _fake_fetch)

    received: list[dict] = []

    def _on_quotes(payload):
        received.append(payload)

    msg_hub.subscribe(Topics.QUOTES_ALL.value, _on_quotes)
    try:
        count = publisher.publish_once()
        assert count == 1
        # 消息总线是异步分发，需要等待送达
        assert _wait_until(lambda: bool(received))
    finally:
        msg_hub.unsubscribe(Topics.QUOTES_ALL.value, _on_quotes)

    assert received
    assert received[0]["600519.SH"]["price"] == 1318.49


def test_akshare_publisher_survives_fetch_error(monkeypatch):
    """拉取失败时发布 0 条而不抛异常。"""
    publisher = AksharePublisher(["600519.SH"])

    def _boom():
        raise RuntimeError("网络错误")

    monkeypatch.setattr(publisher, "_fetch_snapshot", _boom)
    assert publisher.publish_once() == 0


def test_pytdx_symbol_split():
    """Pytdx 市场编号转换。"""
    assert _split_symbol("600519.SH") == (1, "600519")
    assert _split_symbol("000001.SZ") == (0, "000001")
    assert _split_symbol("300750.SZ") == (0, "300750")


def test_pytdx_publisher_reads_env(monkeypatch):
    """连接参数可用环境变量覆盖。"""
    monkeypatch.setenv("PYTDX_HOST", "10.0.0.1")
    monkeypatch.setenv("PYTDX_PORT", "7709")
    monkeypatch.setenv("PYTDX_INTERVAL", "5")
    publisher = PytdxPublisher()
    assert publisher.host == "10.0.0.1"
    assert publisher.port == 7709
    assert publisher.interval == 5.0
