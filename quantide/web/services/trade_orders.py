"""Validate manual orders before dispatching them through a broker."""

import math
from dataclasses import dataclass


@dataclass
class ManualOrder:
    """A validated order in the UI's original units (lots or ten-thousand yuan)."""

    asset: str
    side: str
    price: float
    value: float
    mode: str


def _positive_number(raw, format_error, positive_error):
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(format_error) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(positive_error)
    return value


def _limit_price(form, asset, close_resolver):
    raw = str(form.get("price", "0")).strip()
    price = _positive_number(raw, "价格格式错误", "价格必须大于0") if raw else 0.0
    if price <= 0:
        price = close_resolver(asset)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("价格必须大于0")
    return price


def parse_manual_order(form, close_resolver):
    """Reject ambiguous order instructions and invalid numeric values."""
    asset = str(form.get("asset", "")).strip()
    if not asset:
        raise ValueError("请输入股票代码")
    side, mode = form.get("side", "BUY"), form.get("order_mode", "AMOUNT")
    price_mode = form.get("price_mode", "LIMIT")
    if side not in {"BUY", "SELL"} or mode not in {"AMOUNT", "QUANTITY"}:
        raise ValueError("下单方向或方式无效")
    if price_mode not in {"LIMIT", "MARKET"}:
        raise ValueError("价格模式无效")
    price = _limit_price(form, asset, close_resolver) if price_mode == "LIMIT" else 0.0
    value = _positive_number(form.get("value", "0"), "金额/数量格式错误", "金额/数量必须大于0")
    return ManualOrder(asset, side, price, value, mode)


async def submit_manual_order(broker, order):
    """Preserve broker-native amount sizing and the legacy share fallback."""
    method = "buy" if order.side == "BUY" else "sell"
    if order.mode == "AMOUNT":
        if hasattr(broker, f"{method}_amount"):
            return await getattr(broker, f"{method}_amount")(
                order.asset, order.value * 10000, order.price
            )
        shares = int(order.value * 10000 / order.price) if order.price > 0 else int(order.value)
    else:
        shares = int(order.value) * 100
    return await getattr(broker, method)(order.asset, shares, order.price)


async def submit_share_form(broker, form):
    """Validate and submit the legacy order form whose quantity is in shares."""
    import math

    side = str(form.get("side", "BUY")).upper()
    asset = str(form.get("asset") or "").strip()
    price = float(form.get("price", 0))
    shares = int(form.get("shares", 0))
    if side not in {"BUY", "SELL"} or not asset or shares <= 0:
        raise ValueError("股票、买卖方向和正整数股数为必填项")
    if not math.isfinite(price) or price < 0:
        raise ValueError("价格必须是非负有限数值")
    submit = broker.buy if side == "BUY" else broker.sell
    return await submit(asset, shares, price)
