"""Factor overview page widgets and sorting helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fasthtml.common import A, Div, Form, Option, P, Span, Td, Tr
from fasthtml.common import Button as ActionButton
from fasthtml.common import Input as TextInput
from fasthtml.common import Select as PlainSelect

from quantide.factors.alpha import FEATURE_GROUPS
from quantide.factors.metadata import FactorDefinition
from quantide.service.factor_tracking import TrackingView
from quantide.web.components.data_tables import DatasetTable
from quantide.web.pages.factors_ui import fmt_number, fmt_percent, fmt_time, status_badge

_SORTABLE_COLUMNS = {
    "rank_ic": "RankIC",
    "t": "t 值",
    "days": "有效日",
    "d1d10": "D1-D10",
}
_ATTENTION_STATUSES = {
    "stale_config",
    "stale_data",
    "source_unavailable",
    "failed",
    "insufficient",
    "interrupted",
}
_UNINITIALIZED_STATUSES = {"uninitialized", "disabled"}


def _sort_value(view: TrackingView, key: str) -> float | None:
    current = view.current or {}
    primary = current.get("primary_metrics") or {}
    if key == "rank_ic":
        return (primary.get("rank_ic") or {}).get("mean")
    if key == "t":
        return (primary.get("rank_ic") or {}).get("t_naive")
    if key == "days":
        return (primary.get("rank_ic") or {}).get("n_days")
    if key == "d1d10":
        return (primary.get("deciles") or {}).get("d1_minus_d10")
    return None


def _sort_entries(
    entries: list[tuple[FactorDefinition, TrackingView]], key: str, direction: str
) -> list[tuple[FactorDefinition, TrackingView]]:
    if key not in _SORTABLE_COLUMNS:
        return entries
    present = [entry for entry in entries if _sort_value(entry[1], key) is not None]
    missing = [entry for entry in entries if _sort_value(entry[1], key) is None]
    present = sorted(
        present,
        key=lambda entry: float(_sort_value(entry[1], key) or 0.0),
        reverse=direction != "asc",
    )
    return present + missing


def _overview_url(
    *,
    query: str,
    group_filter: str,
    sort_key: str = "",
    sort_dir: str = "",
) -> str:
    params = {
        "q": query,
        "group": group_filter,
        "sort": sort_key if sort_key in _SORTABLE_COLUMNS else "",
        "dir": sort_dir if sort_dir in {"asc", "desc"} else "",
    }
    params = {key: value for key, value in params.items() if value}
    return f"/factors/?{urlencode(params)}" if params else "/factors/"


def _sort_header(
    label: str,
    key: str,
    *,
    query: str,
    group_filter: str,
    sort_key: str,
    sort_dir: str,
) -> A:
    active = sort_key == key
    next_dir = "asc" if active and sort_dir != "asc" else "desc"
    marker = "↓" if active and sort_dir != "asc" else ("↑" if active else "↕")
    return A(
        Span(label),
        Span(marker, cls="ml-1 text-[10px]", aria_hidden="true"),
        href=_overview_url(
            query=query,
            group_filter=group_filter,
            sort_key=key,
            sort_dir=next_dir,
        ),
        aria_label=f"按{label}排序",
        cls=(
            "inline-flex w-full items-center justify-end gap-1 whitespace-nowrap "
            + ("font-semibold text-[#e41815]" if active else "text-gray-500 hover:text-gray-900")
        ),
    )


def _summary_card(label: str, value: str, *, tone: str = "text-gray-900") -> Div:
    return Div(
        P(label, cls="text-xs text-gray-500"),
        P(value, cls=f"mt-1 text-xl font-semibold tabular-nums {tone}"),
        cls="rounded-xl border border-gray-100 bg-white px-4 py-3 shadow-sm",
    )


def overview_summary(entries: list[tuple[FactorDefinition, TrackingView]]) -> Div:
    total = len(entries)
    normal = sum(1 for _, view in entries if view.status == "normal")
    calculating = sum(1 for _, view in entries if view.status == "calculating")
    attention = sum(1 for _, view in entries if view.status in _ATTENTION_STATUSES)
    uninitialized = sum(1 for _, view in entries if view.status in _UNINITIALIZED_STATUSES)
    updates = [
        fmt_time((view.current or {}).get("computed_at"))
        for _, view in entries
        if (view.current or {}).get("computed_at")
    ]
    latest_update = max(updates) if updates else "—"
    return Div(
        _summary_card("因子总数", str(total)),
        _summary_card("正常", str(normal), tone="text-green-700"),
        _summary_card("计算中", str(calculating), tone="text-gray-700"),
        _summary_card("待处理", str(attention), tone="text-amber-700"),
        _summary_card("未建立", str(uninitialized), tone="text-gray-500"),
        _summary_card("最近更新", latest_update),
        cls="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6",
    )


def _refresh_form(action: str, label: str, *, compact: bool = False) -> Form:
    return Form(
        ActionButton(
            label,
            type="submit",
            cls="q-btn q-btn--sm px-2" if compact else "q-btn h-9 px-3",
            aria_label=label,
            onclick=(
                "this.disabled=true;"
                f"this.textContent='{label}已提交';"
                "this.form.submit();return false;"
            ),
        ),
        method="post",
        action=action,
        cls="inline-flex",
    )


def overview_toolbar(
    *,
    query: str,
    group_filter: str,
    sort_key: str,
    sort_dir: str,
    result_count: int,
    total_count: int,
    is_admin: bool,
) -> Div:
    filter_form = Form(
        TextInput(
            type="search",
            name="q",
            value=query,
            placeholder="筛选结果：名称或 ID",
            aria_label="筛选结果：名称或 ID",
            cls="q-input h-9 w-64",
        ),
        PlainSelect(
            Option("全部分组", value="", selected=not group_filter),
            *[
                Option(group, value=group, selected=group_filter == group)
                for group in FEATURE_GROUPS
            ],
            name="group",
            aria_label="按因子分组筛选",
            cls="q-input h-9 w-56",
        ),
        TextInput(type="hidden", name="sort", value=sort_key),
        TextInput(type="hidden", name="dir", value=sort_dir),
        ActionButton("筛选", type="submit", cls="q-btn q-btn--primary h-9 px-3"),
        (
            A(
                "清除筛选",
                href="/factors/",
                cls="text-sm text-gray-500 hover:text-[#e41815]",
            )
            if query or group_filter
            else None
        ),
        method="get",
        cls="flex flex-wrap items-center gap-2",
        aria_label="因子筛选",
    )
    refresh = _refresh_form("/factors/refresh-all", "刷新全部跟踪") if is_admin else None
    return Div(
        Div(
            P("因子列表", cls="text-base font-semibold text-gray-900"),
            P(
                f"当前 {result_count} / 共 {total_count} 个因子；点击数值列标题可切换排序。",
                cls="mt-1 text-xs text-gray-500",
            ),
        ),
        Div(filter_form, refresh, cls="flex flex-wrap items-center gap-3"),
        cls="flex flex-wrap items-end justify-between gap-4",
    )


def tracking_onboarding(
    entries: list[tuple[FactorDefinition, TrackingView]], *, is_admin: bool
) -> Any:
    if not entries or any(view.current for _, view in entries):
        return None
    action_text = (
        "点击右上角“刷新全部跟踪”，系统会建立首批因子快照。"
        if is_admin
        else "当前还没有跟踪结果；请联系管理员建立首批因子快照。"
    )
    return Div(
        P("还没有因子跟踪结果", cls="text-sm font-medium text-gray-900"),
        P(action_text, cls="mt-1 text-sm text-gray-600"),
        cls="rounded-xl border border-dashed border-gray-300 bg-gray-50 p-4",
    )


def _overview_empty(query: str, group_filter: str) -> Div:
    has_filter = bool(query or group_filter)
    return Div(
        P(
            "没有匹配的因子" if has_filter else "暂无因子",
            cls="text-sm font-medium text-gray-900",
        ),
        P(
            "调整搜索词或分组后重试。" if has_filter else "系统尚未发现可用因子。",
            cls="mt-1 text-sm text-gray-500",
        ),
        (
            A("清除筛选", href="/factors/", cls="mt-3 inline-flex text-sm text-[#e41815]")
            if has_filter
            else None
        ),
        cls="p-10 text-center",
    )


def overview_row(meta: FactorDefinition, view: TrackingView, *, is_admin: bool) -> Any:
    current = view.current or {}
    primary = current.get("primary_metrics") or {}
    rank_ic = primary.get("rank_ic") or {}
    diagnostics = primary.get("diagnostics") or {}
    deciles = primary.get("deciles") or {}
    actions: list[Any] = [
        A(
            "查看",
            href=f"/factors/{meta.factor_id}",
            aria_label=f"查看因子 {meta.display_name}",
            cls="text-sm text-[#e41815] hover:underline",
        )
    ]
    if is_admin:
        actions.append(_refresh_form(f"/factors/{meta.factor_id}/refresh", "刷新", compact=True))
    return Tr(
        Td(
            Div(
                A(
                    meta.display_name,
                    href=f"/factors/{meta.factor_id}",
                    cls="font-medium text-gray-900 hover:text-[#e41815] hover:underline",
                ),
                Div(meta.factor_id, cls="mt-0.5 font-mono text-xs text-gray-500"),
            ),
            cls="min-w-[220px] px-4 py-3",
        ),
        Td(meta.group, cls="hidden px-4 py-3 text-xs text-gray-500 lg:table-cell"),
        Td(status_badge(view.status), cls="px-4 py-3 whitespace-nowrap"),
        Td(
            f"{view.profile.primary_holding_days} 日",
            cls="px-4 py-3 text-right tabular-nums whitespace-nowrap",
        ),
        Td(
            fmt_number(rank_ic.get("mean")),
            cls="px-4 py-3 text-right font-medium tabular-nums whitespace-nowrap",
        ),
        Td(
            fmt_number(rank_ic.get("t_naive"), 2),
            cls="px-4 py-3 text-right tabular-nums whitespace-nowrap",
        ),
        Td(
            str(rank_ic.get("n_days") or 0) if current else "—",
            cls="px-4 py-3 text-right tabular-nums whitespace-nowrap",
        ),
        Td(
            fmt_percent(deciles.get("d1_minus_d10")),
            cls="px-4 py-3 text-right tabular-nums whitespace-nowrap",
        ),
        Td(
            Div(
                P(str(diagnostics.get("as_of_date") or "—"), cls="text-sm text-gray-700"),
                P(
                    f"更新 {fmt_time(current.get('computed_at'))}",
                    cls="mt-0.5 text-xs text-gray-400",
                ),
            ),
            cls="px-4 py-3 text-right whitespace-nowrap",
        ),
        Td(
            Div(*actions, cls="flex items-center justify-end gap-3 whitespace-nowrap"),
            cls="px-4 py-3 text-right",
        ),
    )


def overview_table(
    rows: list[Any],
    *,
    query: str,
    group_filter: str,
    sort_key: str,
    sort_dir: str,
) -> Div:
    if not rows:
        return _overview_empty(query, group_filter)
    sort_args = {
        "query": query,
        "group_filter": group_filter,
        "sort_key": sort_key,
        "sort_dir": sort_dir,
    }
    headers = [
        "因子",
        "分组",
        "状态",
        "持有期",
        _sort_header("RankIC", "rank_ic", **sort_args),
        _sort_header("t", "t", **sort_args),
        _sort_header("有效日", "days", **sort_args),
        _sort_header("D1-D10", "d1d10", **sort_args),
        "数据 / 更新",
        "操作",
    ]
    return Div(
        DatasetTable(
            headers,
            rows,
            column_classes=[
                "",
                "hidden lg:table-cell",
                "",
                "text-right",
                "text-right",
                "text-right",
                "text-right",
                "text-right",
                "text-right",
                "text-right",
            ],
            sticky_header=True,
            aria_label="因子总览列表",
        ),
        cls="overflow-hidden rounded-xl border border-gray-100 bg-white shadow-sm",
    )
