"""Resolve account selections consistently with the current edition boundary."""

from quantide.core.enums import BrokerKind


class LiveAccountUnavailable(ValueError):
    """The selected account requires a live-trading edition."""


def is_live_kind(kind):
    """Accept persisted kind strings and enum values."""
    value = getattr(kind, "value", kind)
    return str(value or "").lower() in {"qmt", "live"}


def account_kinds(allow_live):
    """List usable account kinds in preference order."""
    return [BrokerKind.QMT, BrokerKind.SIMULATION] if allow_live else [BrokerKind.SIMULATION]


def find_broker(registry, kind, account_id):
    """Return a selected broker, retaining explicit invalid selections as failures."""
    if registry is None or not kind or not account_id:
        return None
    try:
        return registry.get(BrokerKind(kind), account_id)
    except Exception:
        return None


def default_account(registry, allow_live):
    """Resolve a default and replace edition-blocked defaults with a simulation."""
    default = registry.get_default() if registry else None
    if not isinstance(default, (tuple, list)) or len(default) != 2:
        return None
    if is_live_kind(default[0]) and not allow_live:
        simulations = registry.list_by_kind(BrokerKind.SIMULATION)
        return (BrokerKind.SIMULATION.value, simulations[0].get("id")) if simulations else None
    return default


def query_account(registry, session, account_id, allow_live):
    """Resolve an explicit account link and remember only a successful lookup."""
    if not account_id or not registry:
        return None
    for kind in account_kinds(allow_live):
        broker = find_broker(registry, kind, account_id)
        if broker is not None:
            session.update(active_account_kind=kind.value, active_account_id=account_id)
            return kind, account_id, broker
    return None


def resolve_active_account(registry, session, query, allow_live):
    """Select a query account, remembered account or usable default."""
    selected = query_account(registry, session, (query or {}).get("account_id"), allow_live)
    if selected:
        return selected
    kind, account_id = session.get("active_account_kind"), session.get("active_account_id")
    if not kind or not account_id or (is_live_kind(kind) and not allow_live):
        default = default_account(registry, allow_live)
        if not default:
            return None
        kind, account_id = default
    broker = find_broker(registry, kind, account_id)
    return (BrokerKind(kind), account_id, broker) if broker else None


def account_details(kind, account_id, broker):
    """Build the shared trading-page account representation."""
    live = is_live_kind(kind)
    return {
        "id": account_id,
        "name": getattr(broker, "portfolio_name", "") or account_id,
        "kind": kind.value,
        "label": "实盘" if live else "仿真",
        "status": getattr(broker, "status", True),
        "is_live": live,
    }


def resolve_order_account(registry, session, form, allow_live):
    """Do not redirect an explicit invalid order selection to another account."""
    explicit_kind = str(form.get("account_kind") or "")
    kind = explicit_kind or str(session.get("active_account_kind") or "")
    account_id = str(form.get("account_id") or session.get("active_account_id") or "")
    if is_live_kind(explicit_kind) and not allow_live:
        raise LiveAccountUnavailable("CE edition 暂不支持实盘交易")
    if not explicit_kind and is_live_kind(kind) and not allow_live:
        kind, account_id = "", ""
    if not kind or not account_id:
        default = default_account(registry, allow_live)
        if not default:
            return None, kind
        kind, account_id = default
    return find_broker(registry, kind, account_id), getattr(kind, "value", kind)


def listed_account(kind, info):
    """Represent a registry listing without fetching a broker or changing its status."""
    return {
        "id": info.get("id"),
        "name": info.get("name") or info.get("id"),
        "kind": kind.value,
        "label": "实盘" if is_live_kind(kind) else "仿真",
        "status": info.get("status", False),
        "is_live": is_live_kind(kind),
    }
