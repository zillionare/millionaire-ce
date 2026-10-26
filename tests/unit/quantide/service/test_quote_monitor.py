"""实时行情监视器测试。"""

from __future__ import annotations

from quantide.service.quote_monitor import QuoteMonitor


def test_monitor_records_latest_quotes_and_stats(monkeypatch):
    monitor = QuoteMonitor()
    callbacks: list = []
    monkeypatch.setattr(
        "quantide.service.quote_monitor.msg_hub.subscribe",
        lambda topic, callback: callbacks.append((topic, callback)),
    )
    monitor.start()
    monitor.start()  # 幂等
    assert len(callbacks) == 1

    monitor._on_quotes(
        {
            "600519.SH": {"name": "贵州茅台", "price": 1334.0, "prev_close": 1335.0, "volume": 100, "amount": 2000},
            "000001.SZ": {"price": 11.2, "prev_close": 11.1, "volume": 200, "amount": 500},
            "BAD": {"price": 0},
        }
    )
    status = monitor.status()
    assert status["symbols"] == 2
    assert status["messages"] == 1
    assert status["updates"] == 2
    assert status["last_batch_size"] == 2

    rows = monitor.snapshot()
    assert [row["symbol"] for row in rows] == ["600519.SH", "000001.SZ"]
    assert rows[0]["name"] == "贵州茅台"
    assert [row["symbol"] for row in monitor.snapshot(query="茅台")] == ["600519.SH"]
    assert [row["symbol"] for row in monitor.snapshot(query="000001")] == ["000001.SZ"]
