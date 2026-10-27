"""系统维护 - 实时行情监控页面。"""

from __future__ import annotations

import datetime
from typing import Any

from fasthtml.common import (
    H2,
    A,
    Button,
    Div,
    Form,
    Input,
    Nav,
    P,
    Span,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
    fast_app,
)

from quantide.data.models.calendar import calendar
from quantide.data.models.stocks import stock_list
from quantide.service.quote_monitor import quote_monitor
from quantide.web.layouts.main import MainLayout
from quantide.web.theme import AppTheme

system_realtime_app, rt = fast_app(hdrs=AppTheme.headers())

_POLL = "every 3s [document.visibilityState === 'visible']"


def _breadcrumb() -> Div:
    return Div(
        Nav(
            A("首页", href="/", cls="hover:text-[#e41815]"),
            Span(">", cls="text-gray-400"),
            A("系统维护", href="/system/", cls="hover:text-[#e41815]"),
            Span(">", cls="text-gray-400"),
            Span("实时行情", cls="text-gray-900 font-medium"),
            cls="flex w-fit items-center justify-start space-x-2 text-sm text-gray-600",
        ),
        cls="mb-4",
    )


def _is_session(now: datetime.datetime) -> bool:
    if not calendar.is_trade_day(now):
        return False
    tm = now.time()
    return datetime.time(9, 30) <= tm <= datetime.time(11, 30) or (
        datetime.time(13, 0) <= tm <= datetime.time(15, 0)
    )


def _status_card(label: str, value: str, hint: str = "", tone: str = "gray") -> Div:
    tone_cls = {
        "green": "text-green-600",
        "amber": "text-gray-600",
        "red": "text-red-600",
        "blue": "text-[#e41815]",
        "gray": "text-gray-900",
    }.get(tone, "text-gray-900")
    return Div(
        P(label, cls="text-xs text-gray-500"),
        P(value, cls=f"mt-1 text-lg font-semibold tabular-nums {tone_cls}"),
        P(hint, cls="mt-1 text-[11px] text-gray-400"),
        cls="bg-white px-4 py-3",
    )


def _freshness(now: datetime.datetime, last: datetime.datetime | None) -> tuple[str, str, str]:
    """返回 (文案, 颜色, 提示)。"""
    if not _is_session(now):
        return "休市", "gray", "非交易时段"
    if last is None:
        return "中断", "red", "尚未收到任何行情"
    age = max(0.0, (now - last).total_seconds())
    if age <= 15:
        return "实时", "green", f"延迟 {age:.0f}s"
    if age <= 60:
        return "延迟", "amber", f"延迟 {age:.0f}s"
    return "中断", "red", f"已 {age:.0f}s 无更新"


def _change_cls(change_pct: float | None) -> str:
    if change_pct is None or change_pct == 0:
        return "text-gray-700"
    return "text-red-500" if change_pct > 0 else "text-green-600"


def _render_rows(now: datetime.datetime, query: str) -> list[Any]:
    rows = quote_monitor.snapshot(query=query, limit=50)
    if not rows:
        return [
            Tr(
                Td(
                    Div(
                        P("尚未收到行情", cls="text-sm font-medium text-gray-500"),
                        P(
                            "请确认 QUANTIDE_QUOTE_PUBLISHER=tencent 已启用，或自定义发布者已连接。",
                            cls="mt-1 text-xs text-gray-400",
                        ),
                        cls="py-12 text-center",
                    ),
                    colspan=9,
                )
            )
        ]
    rendered: list[Any] = []
    for row in rows:
        prev_close = row.get("prev_close")
        change_pct = None
        if prev_close and prev_close > 0:
            change_pct = (row["price"] - prev_close) / prev_close * 100
        received_at = row.get("received_at")
        delay = (
            max(0.0, (now - received_at).total_seconds())
            if isinstance(received_at, datetime.datetime)
            else None
        )
        quote_ts = row.get("quote_ts")
        rendered.append(
            Tr(
                Td(row["symbol"], cls="px-3 py-2 font-mono text-xs text-gray-700"),
                Td(row.get("name") or "-", cls="px-3 py-2 text-gray-700"),
                Td(f"{row['price']:,.2f}", cls="px-3 py-2 text-right tabular-nums"),
                Td(
                    f"{change_pct:+.2f}%" if change_pct is not None else "-",
                    cls=f"px-3 py-2 text-right tabular-nums {_change_cls(change_pct)}",
                ),
                Td(
                    f"{prev_close:,.2f}" if prev_close else "-",
                    cls="px-3 py-2 text-right tabular-nums text-gray-500",
                ),
                Td(
                    f"{row.get('volume'):,.0f}" if row.get("volume") is not None else "-",
                    cls="px-3 py-2 text-right tabular-nums text-gray-600",
                ),
                Td(
                    f"{row.get('amount'):,.0f}" if row.get("amount") is not None else "-",
                    cls="px-3 py-2 text-right tabular-nums text-gray-600",
                ),
                Td(
                    quote_ts.strftime("%H:%M:%S")
                    if isinstance(quote_ts, datetime.datetime)
                    else "-",
                    cls="px-3 py-2 text-right tabular-nums text-gray-500",
                ),
                Td(
                    f"{delay:.1f}s" if delay is not None else "-",
                    cls="px-3 py-2 text-right tabular-nums text-gray-500",
                ),
                cls="border-b border-gray-50 last:border-0",
            )
        )
    return rendered


def _data_health(now: datetime.datetime) -> list[Any]:
    items: list[Any] = []
    try:
        from quantide.data import get_limit_price_store

        limits = get_limit_price_store().limits_map(now.date())
        expected = int(stock_list.size or 0)
        count = len(limits)
        coverage = count / expected if expected else (1.0 if count else 0.0)
        tone = (
            "bg-green-50 text-green-700"
            if coverage >= 0.9
            else ("bg-gray-100 text-gray-700" if coverage >= 0.5 else "bg-red-50 text-red-600")
        )
        label = "涨跌停价已获取" if count else "涨跌停价未获取"
        items.append(
            Span(
                f"{label}：{count}/{expected or '?'}（{coverage * 100:.0f}%）",
                cls=f"rounded-full px-2.5 py-1 text-xs {tone}",
            )
        )
    except Exception:
        items.append(Span("涨跌停价未获取", cls="rounded-full bg-gray-100 px-2.5 py-1 text-xs text-gray-500"))
    items.append(
        A(
            "查看风险事件 →",
            href="/system/risk-events/",
            cls="text-xs font-medium text-[#e41815] hover:text-[#c91412]",
        )
    )
    return items


def _render_content(now: datetime.datetime, query: str) -> Div:
    status = quote_monitor.status()
    last_received = status.get("last_received_at")
    freshness, tone, hint = _freshness(now, last_received)
    last_text = (
        last_received.strftime("%H:%M:%S")
        if isinstance(last_received, datetime.datetime)
        else "—"
    )
    last_batch = (
        status["last_batch_at"].strftime("%H:%M:%S")
        if isinstance(status.get("last_batch_at"), datetime.datetime)
        else "—"
    )
    panel_url = "/system/realtime/panel"
    return Div(
        Div(
            H2("实时行情", cls="text-lg font-semibold text-gray-900"),
            Span(
                "行情源 → MessageHub → 本页；用于确认系统正在接收实时数据",
                cls="text-xs text-gray-500",
            ),
            cls="flex flex-col",
        ),
        Div(
            _status_card("行情状态", freshness, hint, tone),
            _status_card("最后更新", last_text, "行情总线最近一次推送"),
            _status_card("覆盖标的", f"{status['symbols']:,}", "当前缓存的最新快照数"),
            _status_card("累计推送", f"{status['messages']:,}", f"最新批次 {status['last_batch_size']:,} 只"),
            _status_card("最新批次", last_batch, "接收时间"),
            _status_card("行情源", "运行中" if status["started"] else "未启动", "监视器订阅状态", "green" if status["started"] else "red"),
            cls="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-gray-100 bg-gray-100 md:grid-cols-3 xl:grid-cols-6",
        ),
        Div(
            Form(
                Input(
                    name="q",
                    value=query,
                    autocomplete="off",
                    placeholder="输入代码或名称过滤，例如 600519 / 茅台",
                    style="height:2rem;min-height:2rem;box-sizing:border-box;padding-top:0;padding-bottom:0;",
                    cls=(
                        "h-8 w-full max-w-xs rounded-lg border border-gray-200 bg-white "
                        "px-3 text-sm leading-8 outline-none focus:border-[#e41815] "
                        "focus:ring-2 focus:ring-[#e41815]/10"
                    ),
                ),
                Button(
                    "筛选",
                    type="submit",
                    style="height:2rem;min-height:2rem;box-sizing:border-box;",
                    cls=(
                        "inline-flex h-8 w-16 flex-none items-center justify-center whitespace-nowrap "
                        "rounded-lg border border-gray-200 bg-white px-0 text-sm font-medium "
                        "text-gray-600 transition hover:bg-gray-50 hover:text-gray-900"
                    ),
                ),
                hx_get="/system/realtime/panel",
                hx_target="#realtime-panel",
                hx_swap="innerHTML",
                id="realtime-filter",
                cls="flex items-center gap-2",
            ),
            Div(*_data_health(now), cls="flex flex-wrap items-center gap-2"),
            cls="mt-4 flex flex-wrap items-center justify-between gap-3",
        ),
        Div(
            Div(
                Table(
                    Thead(
                        Tr(
                            Th("代码", cls="px-3 py-2 text-left text-xs font-medium text-gray-500"),
                            Th("名称", cls="px-3 py-2 text-left text-xs font-medium text-gray-500"),
                            Th("最新价", cls="px-3 py-2 text-right text-xs font-medium text-gray-500"),
                            Th("涨跌幅", cls="px-3 py-2 text-right text-xs font-medium text-gray-500"),
                            Th("昨收", cls="px-3 py-2 text-right text-xs font-medium text-gray-500"),
                            Th("成交量(手)", cls="px-3 py-2 text-right text-xs font-medium text-gray-500"),
                            Th("成交额(千元)", cls="px-3 py-2 text-right text-xs font-medium text-gray-500"),
                            Th("行情时间", cls="px-3 py-2 text-right text-xs font-medium text-gray-500"),
                            Th("接收延迟", cls="px-3 py-2 text-right text-xs font-medium text-gray-500"),
                            cls="border-b border-gray-100",
                        )
                    ),
                    Tbody(*_render_rows(now, query)),
                    cls="w-full text-sm",
                ),
                cls="overflow-x-auto",
            ),
            P(
                "默认按成交额降序展示前 50 只；完整行情以发布者推送为准。",
                cls="mt-3 text-[11px] text-gray-400",
            ),
            cls="mt-4 rounded-xl border border-gray-100 bg-white p-4 shadow-sm",
        ),
        id="realtime-panel",
        hx_get=panel_url,
        hx_include="#realtime-filter",
        hx_trigger=_POLL,
        hx_swap="innerHTML",
    )


@rt("/")
def index(req, session):
    layout = MainLayout(title="实时行情", user=session.get("auth"))
    layout.set_sidebar_active("/system/realtime")
    layout.main_block = lambda: Div(
        _breadcrumb(),
        Div(
            _render_content(
                datetime.datetime.now(),
                req.query_params.get("q", ""),
            ),
            cls="max-w-[1400px] mx-auto w-full",
        ),
        cls="space-y-6 max-w-[1400px] mx-auto w-full",
    )
    return layout.render(req)


@rt("/panel")
def panel(req):
    return _render_content(
        datetime.datetime.now(),
        req.query_params.get("q", ""),
    )
