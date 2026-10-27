"""实时行情页面渲染测试。"""

from __future__ import annotations

import datetime

from fasthtml.common import to_xml

from quantide.web.pages.system import realtime


class _FakeQuotes:
    def __init__(self, rows=None, last=None) -> None:
        self._rows = rows or []
        self._last = last

    def status(self):
        return {
            "started": True,
            "symbols": len(self._rows),
            "messages": 3,
            "updates": len(self._rows),
            "last_received_at": self._last,
            "last_batch_at": self._last,
            "last_batch_size": len(self._rows),
        }

    def snapshot(self, query="", limit=50):
        return self._rows[:limit]


def test_realtime_content_renders_quotes(monkeypatch):
    now = datetime.datetime(2024, 1, 3, 10, 0)
    monkeypatch.setattr(realtime, "_is_session", lambda current: True)
    monkeypatch.setattr(
        realtime,
        "quote_monitor",
        _FakeQuotes(
            rows=[
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "price": 1334.0,
                    "prev_close": 1335.0,
                    "volume": 100.0,
                    "amount": 2000.0,
                    "quote_ts": now,
                    "received_at": now,
                }
            ],
            last=now,
        ),
    )
    html = to_xml(realtime._render_content(now, ""))
    assert "实时行情" in html
    assert "600519.SH" in html
    assert "贵州茅台" in html
    assert "实时" in html


def test_realtime_content_empty_state(monkeypatch):
    monkeypatch.setattr(realtime, "_is_session", lambda current: True)
    monkeypatch.setattr(realtime, "quote_monitor", _FakeQuotes(rows=[], last=None))
    html = to_xml(realtime._render_content(datetime.datetime(2024, 1, 3, 10, 0), ""))
    assert "尚未收到行情" in html


def test_realtime_filter_survives_polling(monkeypatch):
    """轮询时必须携带筛选输入，避免下一次刷新覆盖用户输入。"""
    now = datetime.datetime(2024, 1, 3, 10, 0)
    monkeypatch.setattr(realtime, "_is_session", lambda current: True)
    monkeypatch.setattr(
        realtime,
        "quote_monitor",
        _FakeQuotes(
            rows=[
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "price": 1334.0,
                    "prev_close": 1335.0,
                    "volume": 100.0,
                    "amount": 2000.0,
                    "quote_ts": now,
                    "received_at": now,
                }
            ],
            last=now,
        ),
    )
    html = to_xml(realtime._render_content(now, "600519"))
    assert 'value="600519"' in html
    assert 'id="realtime-filter"' in html
    assert 'hx-include="#realtime-filter"' in html


def test_realtime_breadcrumb_is_left_aligned():
    from fasthtml.common import to_xml

    html = to_xml(realtime._breadcrumb())
    assert "justify-start" in html
    assert "w-fit" in html


def test_realtime_limit_badge_uses_clear_text(monkeypatch):
    now = datetime.datetime(2024, 1, 3, 10, 0)
    monkeypatch.setattr(realtime, "_is_session", lambda current: True)
    monkeypatch.setattr(realtime, "quote_monitor", _FakeQuotes(rows=[], last=None))

    class _Limits:
        def limits_map(self, _dt):
            return {"600519.SH": (100.0, 120.0), "000001.SZ": (10.0, 12.0)}

    class _Stocks:
        size = 4

    monkeypatch.setattr(
        "quantide.data.get_limit_price_store", lambda: _Limits()
    )
    monkeypatch.setattr(realtime, "stock_list", _Stocks())
    html = to_xml(realtime._render_content(now, ""))
    assert "涨跌停价已获取：2/4（50%）" in html
