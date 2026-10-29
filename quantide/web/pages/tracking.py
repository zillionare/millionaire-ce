"""dry-run 影子 vs 仿真/实盘 对照页。"""

from __future__ import annotations

import json
from typing import Any

from fasthtml.common import *
from starlette.responses import HTMLResponse, RedirectResponse

from quantide.service.strategy_tracking import tracking_store
from quantide.web.layouts.main import MainLayout
from quantide.web.theme import AppTheme

tracking_app, rt = fast_app(hdrs=AppTheme.headers())


def _series(rows: list[dict[str, Any]], source: str) -> dict[str, float]:
    return {
        str(row["date"]): float(row.get("nav") or 0)
        for row in rows
        if row.get("source") == source
    }


def _drawdown(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def tracking_page(req):
    portfolio_id = str(req.path_params.get("portfolio_id") or "")
    runs = [run for run in tracking_store.list_runs() if run.get("portfolio_id") == portfolio_id]
    run = runs[0] if runs else None
    rows = tracking_store.list_daily(portfolio_id=portfolio_id)
    shadow = _series(rows, "dry_run")
    actual = _series(rows, "actual")
    dates = sorted(set(shadow) | set(actual))
    shadow_values = [shadow.get(day) for day in dates]
    actual_values = [actual.get(day) for day in dates]
    diffs = [
        (shadow.get(day) or 0) - (actual.get(day) or 0)
        for day in dates
        if day in shadow and day in actual
    ]

    session = req.scope.get("session", {}) if hasattr(req, "scope") else {}
    layout = MainLayout(title="对照", user=session.get("auth"))
    mode = str(run.get("runtime_id", "")).split(":", 1)[0] if run else ""
    layout.header_active = "实盘" if mode == "live" else "仿真"
    layout.set_sidebar_active("/trade")

    chart_id = f"tracking_chart_{portfolio_id}"
    chart_script = Script(
        f"""
        (function() {{
            var el = document.getElementById('{chart_id}');
            if (!el || typeof echarts === 'undefined') return;
            var chart = echarts.init(el);
            chart.setOption({{
                tooltip: {{ trigger: 'axis' }},
                legend: {{ data: ['dry-run 影子', '实际账户'] }},
                grid: {{ left: 50, right: 20, top: 40, bottom: 30 }},
                xAxis: {{ type: 'category', data: {json.dumps(dates)} }},
                yAxis: {{ type: 'value', scale: true }},
                series: [
                    {{ name: 'dry-run 影子', type: 'line', showSymbol: false,
                       data: {json.dumps(shadow_values)} }},
                    {{ name: '实际账户', type: 'line', showSymbol: false,
                       data: {json.dumps(actual_values)} }}
                ]
            }});
            window.addEventListener('resize', function() {{ chart.resize(); }});
        }})();
        """
    )

    table_rows = []
    for day in dates:
        table_rows.append(
            Tr(
                Td(day),
                Td(f"{shadow[day]:,.2f}" if day in shadow else "-", cls="text-right"),
                Td(f"{actual[day]:,.2f}" if day in actual else "-", cls="text-right"),
                Td(
                    f"{shadow[day] - actual[day]:,.2f}"
                    if day in shadow and day in actual
                    else "-",
                    cls="text-right",
                ),
            )
        )
    if not table_rows:
        table_rows.append(
            Tr(Td("暂无跟踪数据；dry-run 任务会在收盘后生成。", colspan=4, cls="py-8 text-center text-gray-500"))
        )

    latest_model = next((row.get("model_hash") for row in reversed(rows) if row.get("model_hash")), "")
    toggle = (
        Form(
            Button(
                "关闭 dry-run" if run.get("dry_run_enabled") else "开启 dry-run",
                type="submit",
                cls="q-btn q-btn--sm whitespace-nowrap",
            ),
            action=f"/tracking/{portfolio_id}/toggle",
            method="POST",
            cls="inline-flex",
        )
        if run is not None
        else Span("尚未创建跟踪任务", cls="text-xs text-gray-400")
    )
    summary = Div(
        Div(
            H2("dry-run 对照", cls="text-lg font-semibold text-gray-900"),
            Span(
                f"影子模型: {latest_model or '-'}",
                cls="text-xs text-gray-500",
            ),
            cls="flex flex-col gap-1",
        ),
        Div(
            Span(f"dry-run 回撤 {_drawdown([v for v in shadow_values if v is not None]) * 100:.2f}%", cls="text-xs text-gray-600"),
            Span(f"实际回撤 {_drawdown([v for v in actual_values if v is not None]) * 100:.2f}%", cls="text-xs text-gray-600"),
            Span(f"最大差异 {max(diffs) - min(diffs):,.2f}" if diffs else "最大差异 -", cls="text-xs text-gray-600"),
            cls="flex flex-wrap gap-4",
        ),
        toggle,
        cls="flex items-start justify-between gap-4",
    )

    content = Div(
        Div(
            A("← 返回", href="/trade", cls="text-sm text-gray-500 hover:text-gray-900"),
            cls="mb-4",
        ),
        Div(
            summary,
            Div(id=chart_id, cls="mt-4 h-[320px] w-full"),
            Script(src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"),
            chart_script,
            Div(
                Table(
                    Thead(
                        Tr(Th("日期"), Th("dry-run 净值", cls="text-right"), Th("实际净值", cls="text-right"), Th("差异", cls="text-right")),
                        cls="text-xs text-gray-500 border-b",
                    ),
                    Tbody(*table_rows, cls="text-sm"),
                    cls="w-full",
                ),
                cls="mt-4 overflow-x-auto",
            ),
            cls="rounded-xl border border-gray-100 bg-white p-6 shadow-sm",
        ),
        cls="mx-auto w-full max-w-[1200px] p-6",
    )
    layout.main_block = content
    return HTMLResponse(to_xml(layout.render()))


async def toggle_tracking(req):
    """切换 dry-run 开关；模型维护任务不受影响。"""
    portfolio_id = str(req.path_params.get("portfolio_id") or "")
    runs = [run for run in tracking_store.list_runs() if run.get("portfolio_id") == portfolio_id]
    if runs:
        run = runs[0]
        tracking_store.update_run(
            run["runtime_id"],
            dry_run_enabled=not bool(run.get("dry_run_enabled")),
        )
    return RedirectResponse(f"/tracking/{portfolio_id}", status_code=303)


__all__ = ["tracking_app", "tracking_page", "toggle_tracking", "rt"]
