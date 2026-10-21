"""策略预测归因能力的动态加载。

web 层不感知具体模型：策略类通过 ``ATTRIBUTION`` 声明自己支持的归因方式
（当前为 ``"shap"``），并实现 ``explain_prediction(asset, signal_date)``。
本模块负责按回测组合找到策略、取出配置并调用该实现。
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from quantide.data.sqlite import db
from quantide.service.discovery import strategy_loader

ATTRIBUTION_SHAP = "shap"

#: 归因结果缓存（键：组合/资产/决策日/因子集），避免页签来回切换时重复计算
_EXPLAIN_RESULT_CACHE: dict[tuple[Any, ...], dict[str, Any] | None] = {}
#: 因子取值缓存（同样的键），特征页与 SHAP 页共用
_EXPLAIN_FEATURE_CACHE: dict[tuple[Any, ...], list[tuple[str, float]] | None] = {}
_EXPLAIN_RESULT_CACHE_MAX = 64


class _AttributionBroker:
    """归因用的最小 broker 替身（只需要回测区间信息）。"""

    def __init__(self) -> None:
        self._bt_end: datetime.date | None = None
        self.positions: dict[str, Any] = {}
        self.cash: float = 0.0


def attribution_kind(strategy_cls: Any) -> str | None:
    """返回策略声明的归因方式；未声明则返回 ``None``。

    Args:
        strategy_cls: 策略类。

    Returns:
        str | None: 归因方式标识（如 ``"shap"``）。
    """
    if strategy_cls is None:
        return None
    kind = getattr(strategy_cls, "ATTRIBUTION", None)
    if not kind:
        return None
    if not callable(getattr(strategy_cls, "explain_prediction", None)):
        return None
    return str(kind)


def supports_attribution(strategy_cls: Any) -> bool:
    """策略是否支持逐次预测归因。"""
    return attribution_kind(strategy_cls) is not None


def load_backtest_config(portfolio_id: str) -> dict[str, Any]:
    """读取回测实际使用的配置。

    Args:
        portfolio_id: 回测组合 ID。

    Returns:
        dict: 配置字典；缺失时返回空字典。
    """
    portfolio = db.get_portfolio(portfolio_id)
    if portfolio is None:
        return {}
    info = getattr(portfolio, "info", "") or ""
    if not info:
        return {}
    try:
        payload = json.loads(info)
    except (TypeError, ValueError):
        return {}
    if isinstance(payload, dict):
        config = payload.get("config")
        if isinstance(config, dict):
            return config
        return payload
    return {}


def explain_features(
    portfolio_id: str,
    asset: str,
    signal_date: datetime.date,
) -> list[tuple[str, float]] | None:
    """按策略能力动态取该资产在决策日的因子取值。

    Args:
        portfolio_id: 回测组合 ID。
        asset: 证券代码。
        signal_date: 决策日。

    Returns:
        list | None: (因子名, 取值) 列表；策略不支持或数据不足时返回 ``None``。
    """
    portfolio = db.get_portfolio(portfolio_id)
    if portfolio is None:
        return None
    strategy_cls = strategy_loader.load_from_cache().get(portfolio.name or "")
    if strategy_cls is None:
        return None
    if not callable(getattr(strategy_cls, "explain_features", None)):
        return None
    config = load_backtest_config(portfolio_id)
    cache_key = (
        portfolio_id,
        asset,
        signal_date,
        tuple(config.get("features") or ()),
    )
    if cache_key in _EXPLAIN_FEATURE_CACHE:
        return _EXPLAIN_FEATURE_CACHE[cache_key]
    try:
        strategy = strategy_cls(_AttributionBroker(), config)
        explainer = getattr(strategy, "explain_features", None)
        if not callable(explainer):
            return None
        result = explainer(asset, signal_date)
    except Exception:
        return None
    if len(_EXPLAIN_FEATURE_CACHE) >= _EXPLAIN_RESULT_CACHE_MAX:
        _EXPLAIN_FEATURE_CACHE.pop(next(iter(_EXPLAIN_FEATURE_CACHE)))
    _EXPLAIN_FEATURE_CACHE[cache_key] = result
    return result


def explain_prediction(
    portfolio_id: str,
    asset: str,
    signal_date: datetime.date,
) -> dict[str, Any] | None:
    """按策略能力动态计算某笔交易的预测归因。

    Args:
        portfolio_id: 回测组合 ID。
        asset: 证券代码。
        signal_date: 决策日。

    Returns:
        dict | None: 归因结果；策略不支持或数据不足时返回 ``None``。
    """
    portfolio = db.get_portfolio(portfolio_id)
    if portfolio is None:
        return None
    strategy_cls = strategy_loader.load_from_cache().get(portfolio.name or "")
    if not supports_attribution(strategy_cls):
        return None
    config = load_backtest_config(portfolio_id)
    cache_key = (
        portfolio_id,
        asset,
        signal_date,
        tuple(config.get("features") or ()),
    )
    if cache_key in _EXPLAIN_RESULT_CACHE:
        return _EXPLAIN_RESULT_CACHE[cache_key]
    try:
        strategy = strategy_cls(_AttributionBroker(), config)
        explainer = getattr(strategy, "explain_prediction", None)
        if not callable(explainer):
            return None
        result = explainer(asset, signal_date)
    except Exception:
        return None
    if len(_EXPLAIN_RESULT_CACHE) >= _EXPLAIN_RESULT_CACHE_MAX:
        _EXPLAIN_RESULT_CACHE.pop(next(iter(_EXPLAIN_RESULT_CACHE)))
    _EXPLAIN_RESULT_CACHE[cache_key] = result
    return result
