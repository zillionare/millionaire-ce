"""历史委托页面"""

import datetime

from fasthtml.common import *
from monsterui.all import *
from starlette.responses import HTMLResponse

from quantide.data.sqlite import db
from quantide.web.layouts.main import MainLayout
from quantide.web.pages.history_common import display_name, side_label, status_label


def _get_active_account(req) -> tuple[str, str] | None:
    """获取当前活动账户"""
    session = req.scope.get("session", {})
    kind = session.get("active_account_kind")
    account_id = session.get("active_account_id")
    if kind and account_id:
        return kind, account_id
    return None


def DateRangePicker(start_date: str = "", end_date: str = ""):
    """日期范围选择器"""
    today = datetime.date.today().isoformat()
    if not start_date:
        start_date = today
    if not end_date:
        end_date = today

    return Div(
        Div(
            Span("开始日期", cls="text-sm text-gray-600 mr-2"),
            Input(type="date", name="start_date", value=start_date,
                  cls="px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-red-500"),
            cls="flex items-center",
        ),
        Div(
            Span("结束日期", cls="text-sm text-gray-600 mr-2"),
            Input(type="date", name="end_date", value=end_date,
                  cls="px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-red-500"),
            cls="flex items-center ml-4",
        ),
        Button("查询", type="submit",
               cls="ml-4 px-4 py-2 bg-[#e41815] text-white rounded-lg hover:bg-[#c91412]"),
        cls="flex items-center mb-6",
    )


def OrdersTable(orders: list[dict]):
    """委托表格"""
    if not orders:
        return Div(P("暂无委托数据", cls="text-gray-500 text-center py-8"), cls="bg-white rounded-lg shadow")

    status_colors = {
        "已报": "text-gray-600",
        "已成": "text-green-600",
        "已撤": "text-gray-500",
        "部成": "text-gray-600",
        "废单": "text-red-600",
    }

    return Div(
        Table(
            Thead(
                Tr(
                    Th("时间", cls="pb-3 font-medium text-left"),
                    Th("代码", cls="pb-3 font-medium text-left"),
                    Th("名称", cls="pb-3 font-medium text-left"),
                    Th("方向", cls="pb-3 font-medium text-left"),
                    Th("委托价", cls="pb-3 font-medium text-right"),
                    Th("委托量", cls="pb-3 font-medium text-right"),
                    Th("已成量", cls="pb-3 font-medium text-right"),
                    Th("状态", cls="pb-3 font-medium text-center"),
                ),
                cls="text-sm text-gray-600 border-b",
            ),
            Tbody(*[
                Tr(
                    Td(order.get("tm", ""), cls="py-3 text-sm"),
                    Td(order.get("asset", ""), cls="py-3 text-sm font-medium"),
                    Td(order.get("name", ""), cls="py-3 text-sm text-gray-500"),
                    Td(order.get("side", ""), cls=f"py-3 text-sm {'text-red-600' if order.get('side') == '买入' else 'text-green-600'}"),
                    Td(f"{order.get('price', 0):.2f}", cls="py-3 text-sm text-right"),
                    Td(f"{order.get('shares', 0):,}", cls="py-3 text-sm text-right"),
                    Td(f"{order.get('filled', 0):,}", cls="py-3 text-sm text-right"),
                    Td(order.get("status", ""), cls=f"py-3 text-sm text-center {status_colors.get(order.get('status'), 'text-gray-600')}"),
                )
                for order in orders
            ], cls="text-sm"),
            cls="w-full",
        ),
        cls="bg-white rounded-lg shadow overflow-hidden",
    )


def history_orders_list(request):
    """历史委托列表页面"""
    session = request.scope.get("session", {})
    layout = MainLayout(title="历史委托", user=session.get("auth"))
    layout.header_active = "交易"
    layout.set_sidebar_active("/trade/orders/history")

    active = _get_active_account(request)
    if not active:
        layout.main_block = lambda: Div(P("请先选择活动账户", cls="text-gray-500 text-center py-8"), cls="p-6")
        return HTMLResponse(to_xml(layout.render()))

    kind, account_id = active

    start_date = request.query_params.get("start_date", "")
    end_date = request.query_params.get("end_date", "")

    if not start_date:
        start_date = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    if not end_date:
        end_date = datetime.date.today().isoformat()

    orders = []
    try:
        df = db.query_orders_range(
            start=datetime.date.fromisoformat(start_date),
            end=datetime.date.fromisoformat(end_date),
            portfolio_id=account_id,
        )
        if not df.is_empty():
            for row in df.iter_rows(named=True):
                asset = str(row.get("asset", "") or "")
                orders.append({
                    "tm": str(row.get("tm", ""))[:19],
                    "asset": asset,
                    "name": display_name(asset, row.get("name", "")),
                    "side": side_label(row.get("side")),
                    "price": row.get("price") or 0,
                    "shares": row.get("shares") or 0,
                    "filled": row.get("filled") or 0,
                    "status": status_label(row.get("status")),
                })
    except Exception as e:
        print(f"Failed to query orders: {e}")

    def main_block():
        return Div(
            Div(H1("历史委托", cls="text-2xl font-bold text-gray-900"), cls="mb-6"),
            Form(DateRangePicker(start_date, end_date), action="/trade/orders/history", method="GET", cls="mb-6"),
            OrdersTable(orders),
            cls="p-6",
        )

    layout.main_block = main_block
    return HTMLResponse(to_xml(layout.render()))
