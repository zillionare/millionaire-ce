"""历史持仓/委托/成交页面共用的小工具。"""

from __future__ import annotations

from quantide.core.enums import OrderSide, OrderStatus
from quantide.web.components.asset_label import resolve_asset_name

_STATUS_LABELS = {
    OrderStatus.UNREPORTED: "未报",
    OrderStatus.WAIT_REPORTING: "待报",
    OrderStatus.REPORTED: "已报",
    OrderStatus.REPORTED_CANCEL: "已报待撤",
    OrderStatus.PARTSUCC_CANCEL: "部成待撤",
    OrderStatus.PART_CANCEL: "部成已撤",
    OrderStatus.CANCELED: "已撤",
    OrderStatus.PART_SUCC: "部成",
    OrderStatus.SUCCEEDED: "已成",
    OrderStatus.JUNK: "废单",
    OrderStatus.UNKNOWN: "未知",
}

_STATUS_ALIASES = {
    "未报": OrderStatus.UNREPORTED,
    "待报": OrderStatus.WAIT_REPORTING,
    "已报": OrderStatus.REPORTED,
    "已报待撤": OrderStatus.REPORTED_CANCEL,
    "部成待撤": OrderStatus.PARTSUCC_CANCEL,
    "部成已撤": OrderStatus.PART_CANCEL,
    "已撤": OrderStatus.CANCELED,
    "部成": OrderStatus.PART_SUCC,
    "已成": OrderStatus.SUCCEEDED,
    "已成交": OrderStatus.SUCCEEDED,
    "废单": OrderStatus.JUNK,
    "已拒绝": OrderStatus.JUNK,
}


def coerce_status(value) -> OrderStatus:
    """把数据库里的 int/str/enum 状态统一成 ``OrderStatus``。"""
    if isinstance(value, OrderStatus):
        return value
    if isinstance(value, int):
        try:
            return OrderStatus(value)
        except ValueError:
            return OrderStatus.UNKNOWN
    text = str(value).strip()
    if text in _STATUS_ALIASES:
        return _STATUS_ALIASES[text]
    if text.isdigit():
        try:
            return OrderStatus(int(text))
        except ValueError:
            return OrderStatus.UNKNOWN
    upper = text.upper()
    if upper.startswith("ORDERSTATUS."):
        upper = upper.split(".", 1)[1]
    try:
        return OrderStatus[upper]
    except KeyError:
        return OrderStatus.UNKNOWN


def status_label(value) -> str:
    """返回中文委托状态。"""
    return _STATUS_LABELS.get(coerce_status(value), "未知")


def side_label(value) -> str:
    """返回中文买卖方向。"""
    if isinstance(value, OrderSide):
        return "买入" if value == OrderSide.BUY else ("卖出" if value == OrderSide.SELL else "未知")
    text = str(value).strip().upper()
    if text in {"BUY", "1", "买入"}:
        return "买入"
    if text in {"SELL", "-1", "卖出"}:
        return "卖出"
    return "未知"


def display_name(asset: str, fallback: str = "") -> str:
    """优先使用行内名称，否则根据代码解析。"""
    if fallback:
        return str(fallback)
    try:
        return resolve_asset_name(asset)
    except Exception:
        return asset
