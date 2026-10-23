"""A股动量与趋势反转机器学习选股策略 (LightGBM 截面排序).

Universe selection:
- ``market_cap_trim`` (default): drop the largest and smallest 5% by
  point-in-time ``total_mv``, then drop suspended/ST stocks.  All remaining
  stocks are eligible.  Market cap comes from ``~/.quantide/data/daily_basic.parquet``.
- ``liquidity``: legacy top-N by 20-day mean amount.
- In backtest, the universe is re-selected every ``universe_rebalance_days``
  (default 20) trading days; this bounds the cost of refetching the feature
  panel while avoiding a fixed full-period universe.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from quantide.core.enums import FrameType
from quantide.core.strategy import BaseStrategy
from quantide.data.models.calendar import calendar
from quantide.data.models.daily_bars import daily_bars
from quantide.data.models.daily_basic import daily_basic
from quantide.data.models.stocks import stock_list
from quantide.factors.alpha import (
    FACTOR_VERSION,
    FEATURE_COLS,
    _zscore_1d,
    compute_alpha_features,
    standardize_features,
)
from quantide.factors.catalog import resolve_feature_selection

try:
    import lightgbm as lgb

    _HAS_LIGHTGBM = True
except ImportError:  # pragma: no cover
    lgb = None  # type: ignore[assignment]
    _HAS_LIGHTGBM = False

#: 流水线版本（策略源码哈希）：策略逻辑变化时归因缓存自动失效
PIPELINE_VERSION: str = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]

def _bars_fingerprint() -> tuple[Any, ...]:
    """行情数据的轻量指纹（数据更新后使面板缓存失效）。"""
    try:
        return (
            daily_bars.end,
            daily_bars.total_dates,
            daily_bars.last_update_time,
        )
    except Exception:  # pragma: no cover - 数据层未就绪
        return ()


#: 因子面板缓存（键：选池规格 + 因子集 + 信号日 + 持仓 + 数据指纹）。
#: 只保留最近 2 个面板：单个面板约 2000 只 × 80 天 × 45 列 ≈ 55MB，
#: 上限 2 份把常驻内存压在 ~110MB。命中场景：同一进程内重复回测、
#: 以及归因回放反复请求同一面板。
_PANEL_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}
_PANEL_CACHE_MAX = 2

#: 三重障碍标签缓存（键：面板键 + 障碍参数 + 持有期）。
#: 只存训练真正用到的三列（date/asset/tb_ret），单条约 4MB。
_LABEL_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}
_LABEL_CACHE_MAX = 6

#: 归因用的模型缓存（按训练参数 + 因子集 + 截面日 + 持仓键控）
_EXPLAIN_MODEL_CACHE: dict[tuple[Any, ...], tuple[Any, str]] = {}

#: 归因用的截面缓存（只保留当前这一条：约 2000 行、1MB）
_EXPLAIN_CROSS_SECTION_CACHE: dict[tuple[Any, ...], pl.DataFrame] = {}


def _parse_custom_assets(config: dict[str, Any]) -> list[str] | None:
    """Extract custom asset list from strategy config if specified."""
    raw = config.get("symbols") or config.get("universe") or config.get("symbol")
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
        return items or None
    if isinstance(raw, (list, tuple, set)):
        items = [str(s).strip() for s in raw if str(s).strip()]
        return items or None
    return None


def _coerce_date(
    dt: datetime.date | datetime.datetime | str | None,
) -> datetime.date | None:
    """Convert date/datetime/ISO-string into ``datetime.date``."""
    if dt is None:
        return None
    if isinstance(dt, datetime.datetime):
        return dt.date()
    if isinstance(dt, datetime.date):
        return dt
    if isinstance(dt, str):
        s = dt.strip()
        if not s:
            return None
        return datetime.date.fromisoformat(s[:10])
    return None


def _ensure_data_connected() -> bool:
    """Connect local daily_bars/calendar/stock_list if not yet initialized."""
    if getattr(daily_bars, "_initialized", False):
        return True
    default_home = Path("~/.quantide").expanduser()
    cal_path = default_home / "data" / "calendar.parquet"
    bars_path = default_home / "data" / "bars" / "daily"
    stocks_path = default_home / "data" / "stock_list.parquet"
    basic_path = default_home / "data" / "daily_basic.parquet"
    if not cal_path.exists() or not bars_path.exists():
        return False
    try:
        calendar.load(cal_path)
        if stocks_path.exists():
            stock_list.load(stocks_path)
        daily_bars.connect(bars_path, cal_path)
        if basic_path.exists():
            daily_basic.load(basic_path)
        return True
    except Exception:
        return False


def _resolve_stock_name(asset: str) -> str:
    """Resolve human-readable stock name with fallback to asset code."""
    try:
        if stock_list.size > 0:
            name = stock_list.get_name(asset)
            if name:
                return str(name)
    except Exception:
        pass
    return asset


def compute_triple_barrier_labels(
    df: pl.DataFrame,
    horizon: int = 3,
    up_barrier_pct: float = 0.15,
    down_barrier_pct: float = 0.06,
) -> pl.DataFrame:
    """Attach triple-barrier labels to a daily-bar panel.

    The entry reference price is each row's close. Forward bars are scanned up
    to ``horizon`` trading days; the first upper/lower barrier hit determines
    ``tb_ret`` and ``tb_hit``, otherwise the vertical time barrier expires at
    ``horizon`` using the forward close. Rows without a complete forward window
    receive null labels to avoid lookahead bias.

    Same-day dual hits follow ``quantide.service.triple_barrier.evaluate_day``:
    the barrier closer to that day's open wins, with ties resolved to ``down``.

    Args:
        df: Panel with at least ``[asset, date, open, high, low, close]``.
        horizon: Vertical-barrier holding-day limit.
        up_barrier_pct: Upper barrier return, e.g. ``0.15`` for +15%.
        down_barrier_pct: Lower barrier return, e.g. ``0.06`` for -6%.

    Returns:
        ``df`` sorted by ``[asset, date]`` with ``tb_ret``, ``tb_hit``,
        ``tb_hold_days`` and backward-compatible ``fwd_ret`` columns.
    """
    horizon = max(1, int(horizon))
    label_cols = [
        pl.lit(None, dtype=pl.Float64).alias("tb_ret"),
        pl.lit(None, dtype=pl.Utf8).alias("tb_hit"),
        pl.lit(None, dtype=pl.Int64).alias("tb_hold_days"),
        pl.lit(None, dtype=pl.Float64).alias("fwd_ret"),
    ]
    if df.is_empty():
        return df.with_columns(label_cols)

    out = df.sort(["asset", "date"])
    entry_price = pl.col("close").cast(pl.Float64)
    upper_price = entry_price * (1.0 + float(up_barrier_pct))
    lower_price = entry_price * (1.0 - float(down_barrier_pct))
    full_obs = pl.col("close").shift(-horizon).over("asset").is_not_null()

    ret_exprs: list[pl.Expr] = []
    hit_exprs: list[pl.Expr] = []
    day_exprs: list[pl.Expr] = []
    for k in range(1, horizon + 1):
        open_k = pl.col("open").cast(pl.Float64).shift(-k).over("asset")
        high_k = pl.col("high").cast(pl.Float64).shift(-k).over("asset")
        low_k = pl.col("low").cast(pl.Float64).shift(-k).over("asset")

        hit_up = high_k >= upper_price
        hit_down = low_k <= lower_price
        up_dist = (open_k - upper_price).abs()
        down_dist = (open_k - lower_price).abs()
        win_up = hit_up & (~hit_down | (up_dist < down_dist))
        win_down = hit_down & (~hit_up | ~(up_dist < down_dist))

        ret_exprs.append(
            pl.when(win_up)
            .then(pl.lit(float(up_barrier_pct)))
            .when(win_down)
            .then(pl.lit(-float(down_barrier_pct)))
            .otherwise(pl.lit(None, dtype=pl.Float64))
        )
        hit_exprs.append(
            pl.when(win_up)
            .then(pl.lit("up", dtype=pl.Utf8))
            .when(win_down)
            .then(pl.lit("down", dtype=pl.Utf8))
            .otherwise(pl.lit(None, dtype=pl.Utf8))
        )
        day_exprs.append(
            pl.when(win_up | win_down)
            .then(pl.lit(k, dtype=pl.Int64))
            .otherwise(pl.lit(None, dtype=pl.Int64))
        )

    forward_close = pl.col("close").cast(pl.Float64).shift(-horizon).over("asset")
    expire_ret = forward_close / entry_price - 1.0
    expire_hit = pl.lit("expire", dtype=pl.Utf8)
    expire_days = pl.lit(horizon, dtype=pl.Int64)

    tb_ret = (
        pl.when(full_obs)
        .then(pl.coalesce([*ret_exprs, expire_ret]))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("tb_ret")
    )
    tb_hit = (
        pl.when(full_obs)
        .then(pl.coalesce([*hit_exprs, expire_hit]))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
        .alias("tb_hit")
    )
    tb_days = (
        pl.when(full_obs)
        .then(pl.coalesce([*day_exprs, expire_days]))
        .otherwise(pl.lit(None, dtype=pl.Int64))
        .alias("tb_hold_days")
    )
    return out.with_columns([tb_ret, tb_hit, tb_days]).with_columns(
        pl.col("tb_ret").alias("fwd_ret")
    )


def _fit_ml_model(
    hist_df: pl.DataFrame,
    train_window: int = 45,
    horizon: int = 3,
    use_lightgbm: bool = True,
    up_barrier_pct: float = 0.15,
    down_barrier_pct: float = 0.06,
    feature_cols: list[str] | None = None,
    label_cache_key: tuple[Any, ...] | None = None,
) -> tuple[Any, str]:
    """Train the ranking model strictly on leak-free historical samples.

    Labels are triple-barrier outcomes computed from ``hist_df``. The caller
    MUST still filter ``date <= signal_date`` so shifted forward bars cannot
    observe future prices.

    Args:
        hist_df: 截至信号日的标准化特征面板。
        train_window: 训练窗口（交易日）。
        horizon: 标签的前瞻持有交易日。
        use_lightgbm: True 用 LightGBM，False 用岭回归。
        up_barrier_pct: 标签的止盈障碍。
        down_barrier_pct: 标签的止损障碍。
        feature_cols: 参与训练的因子；为空表示全部因子。
        label_cache_key: 面板内容键；相同面板的标签可跨训练复用。

    Returns:
        tuple: (模型, 引擎名)。
    """
    z_cols = [f"{c}_z" for c in (feature_cols or FEATURE_COLS)]
    label_key = (
        tuple(label_cache_key) if label_cache_key is not None else id(hist_df),
        round(float(up_barrier_pct), 6),
        round(float(down_barrier_pct), 6),
        int(horizon),
    )
    labeled = _LABEL_CACHE.get(label_key)
    if labeled is None:
        labeled = compute_triple_barrier_labels(
            hist_df,
            horizon=horizon,
            up_barrier_pct=up_barrier_pct,
            down_barrier_pct=down_barrier_pct,
        )
        if len(_LABEL_CACHE) >= _LABEL_CACHE_MAX:
            _LABEL_CACHE.pop(next(iter(_LABEL_CACHE)))
        # 只保留训练用得到的列，避免整面板副本放大内存占用
        _LABEL_CACHE[label_key] = labeled.select(["date", "asset", "tb_ret"])
    valid_train = labeled.filter(
        pl.col("tb_ret").is_not_null() & pl.col("tb_ret").is_finite()
    )
    if valid_train.is_empty():
        raise RuntimeError("训练样本不足：没有可用的三重障碍标签")

    train_dates = valid_train["date"].unique().sort().tail(max(5, train_window))
    valid_train = valid_train.filter(pl.col("date").is_in(train_dates.to_list()))
    if valid_train.is_empty():
        raise RuntimeError("训练样本不足：训练窗口内没有可用的标签")

    n_assets = valid_train["asset"].n_unique()
    if n_assets > 1:
        valid_train = valid_train.with_columns(
            (
                pl.col("tb_ret").rank().over("date")
                / pl.len().over("date").clip(lower_bound=1)
                - 0.5
            ).alias("label_y")
        )
    else:
        valid_train = valid_train.with_columns(
            (pl.col("tb_ret").clip(-0.2, 0.2) * 2.5).alias("label_y")
        )

    x_train = (
        valid_train.select(z_cols)
        .fill_null(0.0)
        .fill_nan(0.0)
        .to_numpy()
        .astype(np.float64)
    )
    y_train = (
        valid_train["label_y"]
        .fill_null(0.0)
        .fill_nan(0.0)
        .to_numpy()
        .astype(np.float64)
    )

    if use_lightgbm and _HAS_LIGHTGBM and len(x_train) >= 30:
        min_child = max(5, min(20, len(x_train) // 10))
        model = lgb.LGBMRegressor(
            n_estimators=50,
            max_depth=4,
            num_leaves=15,
            learning_rate=0.05,
            min_child_samples=min_child,
            subsample_for_bin=5000,
            random_state=42,
            verbosity=-1,
            n_jobs=1,
        )
        model.fit(x_train, y_train)
        return model, "LightGBM"

    if len(x_train) >= 8:
        n_feat = x_train.shape[1]
        reg = 10.0 * np.eye(n_feat, dtype=np.float64)
        ridge_w = np.linalg.solve(x_train.T @ x_train + reg, x_train.T @ y_train)
        return ridge_w, "Prior-Ridge"

    return None, "Prior-Ridge"


def _score_cross_section(
    pred_df: pl.DataFrame,
    model: Any,
    model_engine: str,
    feature_cols: list[str] | None = None,
) -> np.ndarray:
    """预测目标日截面的模型得分（截面 z-score，不含任何手工先验）。"""
    cols = feature_cols or FEATURE_COLS
    if pred_df.is_empty():
        return np.array([], dtype=np.float64)

    z_cols = [f"{c}_z" for c in cols]
    x_pred = (
        pred_df.select(z_cols)
        .fill_null(0.0)
        .fill_nan(0.0)
        .to_numpy()
        .astype(np.float64)
    )

    if model is None:
        raise RuntimeError("模型不可用，无法计算截面得分")

    if model_engine == "LightGBM" and hasattr(model, "predict"):
        raw_pred = np.asarray(model.predict(x_pred), dtype=np.float64)
        return _zscore_1d(raw_pred) if len(x_pred) > 1 else raw_pred

    if isinstance(model, np.ndarray):
        ridge_pred = x_pred @ model
        return _zscore_1d(ridge_pred) if len(x_pred) > 1 else ridge_pred

    raise RuntimeError(f"未知的模型引擎: {model_engine}")


def _describe_kline_pattern(row: dict[str, Any]) -> str:
    """Generate a human-readable candlestick/trend pattern tag for a stock."""
    low_open_bull = float(row.get("low_open_bull") or 0.0)
    overnight_gap = float(row.get("overnight_gap") or 0.0)
    intraday_ret = float(row.get("intraday_ret") or 0.0)
    ma20_bounce = float(row.get("ma20_support_bounce") or 0.0)
    lower_shadow = float(row.get("lower_shadow") or 0.0)
    ret_1d = float(row.get("ret_1d") or 0.0)
    vol_ratio = float(row.get("vol_ratio_5_20") or 1.0)
    intra_vs_over = float(row.get("intraday_vs_overnight_5d") or 0.0)
    clv_flow = float(row.get("clv_money_flow") or 0.0)
    rev_shrink = float(row.get("rev_5d_shrink_vol") or 0.0)
    ma_align = float(row.get("ma_alignment") or 0.0)
    ma20_slope = float(row.get("ma20_slope") or 0.0)

    if low_open_bull > 0.0004 or (overnight_gap < -0.004 and intraday_ret > 0.012):
        return "低开阳线弱转强"
    if ma20_bounce > 0.5:
        return "均线回踩支撑"
    if lower_shadow >= 0.42 and intraday_ret >= -0.005:
        return "长下影探底"
    if ret_1d >= 0.035 and vol_ratio >= 1.15:
        return "放量突破动量"
    if intra_vs_over > 0.025 and clv_flow > 0.15:
        return "日内主力推升"
    if rev_shrink > 0.015 and intraday_ret > 0.0:
        return "缩量回调企稳"
    if ma_align >= 0.66 and ma20_slope > 0.0:
        return "多头趋势稳健"
    return "量价共振向上"


def _describe_signal_reason(row: dict[str, Any]) -> str:
    """Build a concise 3-part explanation string for the selected stock."""
    parts: list[str] = []

    rs20 = float(row.get("rs_board_20d") or 0.0)
    rs5 = float(row.get("rs_board_5d") or 0.0)
    if abs(rs20) >= abs(rs5) or rs20 > 0:
        parts.append(f"板块超额{rs20 * 100:+.1f}%")
    else:
        parts.append(f"5日超额{rs5 * 100:+.1f}%")

    ma20_bounce = float(row.get("ma20_support_bounce") or 0.0)
    ma20_slope = float(row.get("ma20_slope") or 0.0)
    ma_align = float(row.get("ma_alignment") or 0.0)
    sharpe_mom = float(row.get("sharpe_mom_20d") or 0.0)
    if ma20_bounce > 0.5:
        parts.append("MA20回踩支撑")
    elif ma20_slope > 0.0:
        parts.append(f"MA20斜率向上({ma20_slope * 100:+.1f}%)")
    elif ma_align >= 0.66:
        parts.append("均线多头排列")
    else:
        parts.append(f"夏普动量{sharpe_mom:+.2f}")

    clv_flow = float(row.get("clv_money_flow") or 0.0)
    intra_vs_over = float(row.get("intraday_vs_overnight_5d") or 0.0)
    resilience = float(row.get("downside_resilience") or 0.0)
    if clv_flow > 0.0:
        parts.append("日内资金净流入")
    elif intra_vs_over > 0.0:
        parts.append("盘中主力承接")
    elif resilience > 0.0:
        parts.append("逆市抗跌韧性")
    else:
        parts.append("短线反转修复")

    return " | ".join(parts)


def _format_candidate_rows(
    scored_df: pl.DataFrame,
    target_date: datetime.date,
    model_engine: str,
    limit: int,
    take_profit_pct: float = 0.15,
    stop_loss_pct: float = 0.06,
    horizon: int = 3,
) -> list[dict[str, Any]]:
    """Format top-N scored rows into UI & strategy selection dicts."""
    if scored_df.is_empty():
        return []

    take_profit_pct = float(take_profit_pct)
    stop_loss_pct = float(stop_loss_pct)
    barrier_horizon = int(horizon)

    # Exclude stocks locked at up-limit on the signal bar
    tradable = scored_df.filter(
        ~((pl.col("up_limit") > 0) & (pl.col("close") >= pl.col("up_limit") * 0.999))
    )
    if tradable.is_empty():
        tradable = scored_df

    ranked = tradable.sort(["ml_score", "asset"], descending=[True, False]).head(limit)
    date_str = target_date.isoformat()

    results: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked.iter_rows(named=True), start=1):
        asset = str(row["asset"])
        close_val = float(row.get("close") or 0.0)
        tp_price = round(close_val * (1.0 + take_profit_pct), 2)
        sl_price = round(close_val * (1.0 - stop_loss_pct), 2)
        tb_barrier_text = (
            f"止盈:{tp_price:.2f}(+{take_profit_pct * 100:.0f}%) / "
            f"止损:{sl_price:.2f}(-{stop_loss_pct * 100:.0f}%) / "
            f"{barrier_horizon}日"
        )
        ma_bounce = float(row.get("ma20_support_bounce") or 0.0)
        ma_align = float(row.get("ma_alignment") or 0.0)
        ma20_slp = float(row.get("ma20_slope") or 0.0)
        ma_support_val = round(
            ma_bounce + 0.5 * ma_align + 5.0 * max(ma20_slp, 0.0), 4
        )
        results.append(
            {
                "rank": idx,
                "asset": asset,
                "name": _resolve_stock_name(asset),
                "board": str(row.get("board") or "其他"),
                "close": round(close_val, 4),
                "ret_1d": round(float(row.get("ret_1d") or 0.0), 4),
                "ret_5d": round(float(row.get("ret_5d") or 0.0), 4),
                "ret_20d": round(float(row.get("ret_20d") or 0.0), 4),
                "ml_score": round(float(row.get("ml_score") or 0.0), 4),
                "rs_board_20d": round(float(row.get("rs_board_20d") or 0.0), 4),
                "ma_support": ma_support_val,
                "kline_pattern": _describe_kline_pattern(row),
                "signal_reason": _describe_signal_reason(row),
                "as_of_date": date_str,
                "model_engine": model_engine,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "barrier_horizon": barrier_horizon,
                "tb_barrier_text": tb_barrier_text,
            }
        )
    return results


def _stable_universe_rank(asset: str, mode: str) -> str:
    """返回资产的稳定随机序（与调仓日无关，仅依赖代码与模式）。

    Args:
        asset: 证券代码。
        mode: 选池模式。

    Returns:
        str: 十六进制哈希串，可直接用于排序。
    """
    digest = hashlib.blake2b(
        f"{mode}:{asset}".encode(), digest_size=8
    ).hexdigest()
    return digest


_INDEX_POOL_CACHE: dict[tuple[tuple[str, ...], datetime.date], set[str]] = {}


def _index_pit_universe(
    target_date: datetime.date,
    codes: list[str] | None,
) -> set[str]:
    """读取指数成分 PIT 快照（按代码+日期缓存）。"""
    codes = [str(c).strip() for c in (codes or []) if str(c).strip()]
    if not codes:
        codes = ["000300.SH", "000905.SH", "000852.SH"]
    key = (tuple(codes), target_date)
    cached = _INDEX_POOL_CACHE.get(key)
    if cached is not None:
        return cached
    pool: set[str] = set()
    try:
        from quantide.data import get_index_weight_store

        store = get_index_weight_store()
        for code in codes:
            try:
                frame = store.constituents(code, target_date, include_weight=False)
            except Exception:
                continue
            if frame is not None and not frame.is_empty() and "con_code" in frame.columns:
                pool.update(frame["con_code"].to_list())
    except Exception:
        pool = set()
    if len(_INDEX_POOL_CACHE) >= 256:
        _INDEX_POOL_CACHE.clear()
    _INDEX_POOL_CACHE[key] = pool
    return pool


def _cap_universe(
    cap: pl.DataFrame,
    universe_size: int,
    holdings: list[str] | None,
    mode: str,
) -> list[str]:
    """按稳定哈希把可选池精确裁剪到 universe_size（持仓优先保留）。"""
    assets = sorted(cap["asset"].unique().to_list())
    if universe_size > 0 and len(assets) > universe_size:
        held = [a for a in (holdings or []) if a in set(assets)]
        ranked = sorted(
            [a for a in assets if a not in set(held)],
            key=lambda asset: _stable_universe_rank(asset, mode),
        )
        picked = held + ranked[: max(0, universe_size - len(held))]
        cap = cap.filter(pl.col("asset").is_in(picked))
    return sorted(cap["asset"].unique().to_list())


def _filter_universe(
    bars_df: pl.DataFrame,
    target_date: datetime.date,
    universe_size: int,
    mode: str = "market_cap_trim",
    trim_pct: float = 0.05,
    holdings: list[str] | None = None,
    index_codes: list[str] | None = None,
) -> list[str]:
    """Select the tradable universe on ``target_date``.

    ``market_cap_trim`` first removes the largest and smallest ``trim_pct`` of
    the cross-section by ``total_mv``, then removes suspended and ST stocks
    from the remaining set.  The current holdings are always kept (as long as
    they are still eligible) so they can be ranked and sold by the alpha rule
    instead of being force-exited as "未入池"; the remaining slots are filled
    by a per-asset stable hash (exact size, no rotation with the rebalance
    date, uncorrelated with market cap; ``universe_size <= 0`` means no cap).
    ``liquidity`` keeps the legacy top-N by 20-day mean amount.
    ``index_pit`` restricts the pool to index constituents as of
    ``target_date`` (point-in-time, no look-ahead); it falls back to
    ``market_cap_trim`` when the constituent store has no coverage.
    """
    on_date = bars_df.filter(pl.col("date") == target_date)
    if on_date.is_empty():
        return []

    cols = set(on_date.columns)

    def _drop_st_names(frame: pl.DataFrame) -> pl.DataFrame:
        if frame.is_empty() or stock_list.size == 0:
            return frame
        if "name" not in stock_list.data.columns:
            return frame
        try:
            st_names = set(
                stock_list.data.filter(
                    pl.col("name").str.to_uppercase().str.contains("ST")
                    | pl.col("name").str.contains("退")
                )["asset"].to_list()
            )
            if st_names:
                return frame.filter(~pl.col("asset").is_in(st_names))
        except Exception:
            pass
        return frame

    if mode == "index_pit":
        pool = _index_pit_universe(target_date, index_codes)
        if pool:
            cap = on_date.filter(pl.col("asset").is_in(pool))
            cap = cap.filter(pl.col("volume") > 0)
            if "is_st" in cols:
                cap = cap.filter(~pl.col("is_st").fill_null(False))
            cap = _drop_st_names(cap)
            if cap.is_empty():
                return []
            return _cap_universe(
                cap, universe_size, holdings, mode
            )
        # 成分数据缺失时退回市值裁剪，避免策略失效。
        mode = "market_cap_trim"

    if mode == "market_cap_trim" and "total_mv" in cols:
        cap = on_date.filter(
            (pl.col("close") > 0)
            & pl.col("total_mv").is_not_null()
            & (pl.col("total_mv") > 0)
        )
        if cap.height > 0:
            # 按数量精确剔除两端各 trim_pct，避免 quantile 插值导致多剔除。
            cap = cap.sort(["total_mv", "asset"])
            n_trim = int(cap.height * trim_pct)
            if n_trim > 0:
                cap = cap.slice(n_trim, max(0, cap.height - 2 * n_trim))
            # 市值裁剪后再排除停牌、ST。
            cap = cap.filter(pl.col("volume") > 0)
            if "is_st" in cols:
                cap = cap.filter(~pl.col("is_st").fill_null(False))
            cap = _drop_st_names(cap)
            # 选池：先放入当前持仓（仍合格的），其余按「每资产稳定哈希」补足：
            # - 规模精确等于 universe_size；
            # - 持仓一定在池内 → 不会被「未入池」强卖，可按排名正常卖出；
            # - 非持仓成员的顺序与调仓日无关 → 不会整池轮换；
            # - 与市值大小无关，避免市值偏置。
            return _cap_universe(cap, universe_size, holdings, mode)

    cond = (pl.col("close") > 0) & (pl.col("volume") > 0)
    if "is_st" in cols:
        cond = cond & (~pl.col("is_st").fill_null(False))
    eligible = on_date.filter(cond)
    eligible = _drop_st_names(eligible)
    if eligible.is_empty():
        return []

    assets = eligible["asset"].unique().to_list()
    if universe_size <= 0 or len(assets) <= universe_size:
        return sorted(assets)

    recent_dates = bars_df["date"].unique().sort().tail(20).to_list()
    amt_expr = (
        pl.col("amount")
        if "amount" in cols
        else (pl.col("volume") * pl.col("close"))
    )
    top_liquid = (
        bars_df.filter(
            pl.col("date").is_in(recent_dates)
            & pl.col("asset").is_in(assets)
        )
        .group_by("asset")
        .agg(amt_expr.mean().alias("avg_amt"))
        .sort(["avg_amt", "asset"], descending=[True, False])
        .head(universe_size)
    )
    return top_liquid["asset"].to_list()


def _filter_liquid_universe(
    bars_df: pl.DataFrame,
    target_date: datetime.date,
    universe_size: int,
) -> list[str]:
    """Backward-compatible liquidity-only universe filter."""
    return _filter_universe(
        bars_df, target_date, universe_size, mode="liquidity"
    )


class MLMomentumReversalStrategy(BaseStrategy):
    """A股动量与趋势反转机器学习选股策略 (LightGBM 截面排序)"""

    VERSION = "v1.0.0"
    RISK_OVERLAY = "triple_barrier"
    #: 声明本策略支持逐次预测归因（web 层据此动态加载 SHAP 面板）
    ATTRIBUTION = "shap"
    PARAMS: dict[str, Any] = {
        "top_k": 5,
        "keep_top_n": 15,
        "universe_size": 2000,
        "universe_mode": "market_cap_trim",
        "market_cap_trim_pct": 0.05,
        "universe_rebalance_days": 20,
        "train_window": 45,
        "horizon": 3,
        "retrain_days": 5,
        "stop_loss_pct": 0.06,
        "take_profit_pct": 0.15,
        "slippage_pct": 0.001,
        "use_lightgbm": True,
        "use_broker_barriers": True,
        "use_alpha_exit": True,
        "fill_timing": "auction_close",
        "auction_epsilon": 0.001,
        "index_pool_codes": ["000300.SH", "000905.SH", "000852.SH"],
    }
    params = PARAMS

    @staticmethod
    def default_config() -> dict[str, Any]:
        """Return default strategy parameters."""
        return dict(MLMomentumReversalStrategy.PARAMS)

    def __init__(self, broker: Any, config: dict[str, Any] | None = None) -> None:
        merged = {**self.default_config(), **(config or {})}
        super().__init__(broker, merged)
        self.top_k = max(1, int(self.config.get("top_k", 5)))
        self.keep_top_n = max(self.top_k, int(self.config.get("keep_top_n", 15)))
        self.universe_size = max(
            self.keep_top_n, int(self.config.get("universe_size", 2000))
        )
        self.universe_mode = str(
            self.config.get("universe_mode", "market_cap_trim")
        ).strip()
        self.index_pool_codes = list(
            self.config.get("index_pool_codes")
            or ["000300.SH", "000905.SH", "000852.SH"]
        )
        self.live_min_quote_coverage = float(
            self.config.get("live_min_quote_coverage", 0.98)
        )
        self.market_cap_trim_pct = min(
            0.5,
            max(0.0, float(self.config.get("market_cap_trim_pct", 0.05))),
        )
        self.universe_rebalance_days = max(
            1, int(self.config.get("universe_rebalance_days", 20))
        )
        self.train_window = max(10, int(self.config.get("train_window", 45)))
        self.horizon = max(1, int(self.config.get("horizon", 3)))
        self.retrain_days = max(1, int(self.config.get("retrain_days", 5)))
        self.stop_loss_pct = float(self.config.get("stop_loss_pct", 0.06))
        self.take_profit_pct = float(self.config.get("take_profit_pct", 0.15))
        self.use_lightgbm = bool(self.config.get("use_lightgbm", True))
        self.use_broker_barriers = bool(
            self.config.get("use_broker_barriers", True)
        )
        # alpha 排名卖出开关：关闭后只剩三重障碍出场
        self.use_alpha_exit = bool(self.config.get("use_alpha_exit", True))
        if not self.use_alpha_exit and not self.use_broker_barriers:
            self.log(
                "use_alpha_exit 与 use_broker_barriers 同时关闭，持仓将没有自动卖出条件",
                level="WARNING",
            )
        self.selected_features: list[str] = resolve_feature_selection(
            self.config.get("features")
        )
        self.custom_assets = _parse_custom_assets(self.config)

        self._model: Any = None
        self._model_engine: str = "LightGBM" if self.use_lightgbm else "Prior-Ridge"
        #: 归因用：回测期间的每日持仓快照（日期 → 资产 → 股数）
        self._positions_by_date: dict[datetime.date, dict[str, float]] = {}
        #: 重训计数按「交易日」推进（实盘每 2 秒一次 on_bar 不应算作一天）
        self._days_since_train: int = 0
        self._last_train_day: datetime.date | None = None
        #: 已执行决策的信号日（实盘按日去重，避免重复下单）
        self._last_executed_signal_date: datetime.date | None = None
        #: 实盘行情缺失标志（置 True 时本次 on_bar 不交易，下次重试）
        self._data_missing: bool = False
        self._cached_norm_panel: pl.DataFrame | None = None
        self._panel_start: datetime.date | None = None
        self._panel_end: datetime.date | None = None

    async def init(self) -> None:
        """Initialize strategy state and log configuration."""
        self.log(
            f"MLMomentumReversalStrategy Initialized: top_k={self.top_k}, "
            f"keep_top_n={self.keep_top_n}, universe_size={self.universe_size}, "
            f"universe_mode={self.universe_mode}, "
            f"market_cap_trim_pct={self.market_cap_trim_pct}, "
            f"train_window={self.train_window}, horizon={self.horizon}, "
            f"use_lightgbm={self.use_lightgbm}, "
            f"use_alpha_exit={self.use_alpha_exit}"
        )

    @classmethod
    def select_stocks(
        cls,
        as_of_date: datetime.date | datetime.datetime | str | None = None,
        config: dict[str, Any] | None = None,
        bars_df: pl.DataFrame | None = None,
    ) -> list[dict[str, Any]]:
        """Run alpha feature engineering and LightGBM ranking for ``as_of_date``.

        Args:
            as_of_date: Target signal date (None uses latest available date).
            config: Optional parameter overrides.
            bars_df: Optional pre-loaded daily bars DataFrame for testing/caching.

        Returns:
            List of top-K ranked stock dictionaries with scores and explanations.
        """
        cfg = {**cls.default_config(), **(config or {})}
        top_k = max(1, int(cfg.get("top_k", 5)))
        universe_size = max(top_k, int(cfg.get("universe_size", 2000)))
        train_window = max(10, int(cfg.get("train_window", 45)))
        horizon = max(1, int(cfg.get("horizon", 3)))
        take_profit_pct = float(cfg.get("take_profit_pct", 0.15))
        stop_loss_pct = float(cfg.get("stop_loss_pct", 0.06))
        use_lightgbm = bool(cfg.get("use_lightgbm", True))
        universe_mode = str(
            cfg.get("universe_mode", "market_cap_trim")
        ).strip()
        market_cap_trim_pct = min(
            0.5, max(0.0, float(cfg.get("market_cap_trim_pct", 0.05)))
        )
        custom_assets = _parse_custom_assets(cfg)
        req_date = _coerce_date(as_of_date)

        if bars_df is None:
            if not _ensure_data_connected():
                return []
            store_end = (
                getattr(daily_bars, "max_date", None)
                or getattr(daily_bars, "end", None)
                or getattr(calendar, "end", None)
                or datetime.date(2024, 12, 31)
            )
            query_end = req_date or store_end
            if isinstance(store_end, datetime.date) and query_end > store_end:
                query_end = store_end
            lookback = max(train_window + 30, 75)
            raw_df = daily_bars.get_bars(
                n=lookback,
                end=query_end,
                assets=custom_assets,
                adjust=None,
                eager_mode=True,
            )
            assert isinstance(raw_df, pl.DataFrame)
            bars_df = raw_df

        if bars_df.is_empty():
            return []

        panel = bars_df.with_columns(pl.col("date").cast(pl.Date))
        if custom_assets:
            panel = panel.filter(pl.col("asset").is_in(custom_assets))
        if req_date is not None:
            panel = panel.filter(pl.col("date") <= req_date)
        if panel.is_empty():
            return []

        target_date: datetime.date = panel["date"].max()  # type: ignore[assignment]
        if custom_assets:
            candidates = sorted(panel["asset"].unique().to_list())
        else:
            panel = daily_basic.attach(panel)
            candidates = _filter_universe(
                panel,
                target_date,
                universe_size,
                mode=universe_mode,
                trim_pct=market_cap_trim_pct,
                index_codes=cfg.get("index_pool_codes"),
            )
        if not candidates:
            return []

        panel = panel.filter(pl.col("asset").is_in(candidates))
        stock_meta = stock_list.data if stock_list.size > 0 else None
        feature_cols = resolve_feature_selection(cfg.get("features")) or FEATURE_COLS
        feat_df = compute_alpha_features(panel, stock_meta=stock_meta)
        norm_df = standardize_features(feat_df, feature_cols)

        model, model_engine = _fit_ml_model(
            norm_df,
            train_window=train_window,
            horizon=horizon,
            use_lightgbm=use_lightgbm,
            up_barrier_pct=take_profit_pct,
            down_barrier_pct=stop_loss_pct,
            feature_cols=feature_cols,
        )
        cross_df = norm_df.filter(pl.col("date") == target_date)
        if cross_df.is_empty():
            return []

        scores = _score_cross_section(cross_df, model, model_engine, feature_cols)
        scored_df = cross_df.with_columns(pl.Series("ml_score", scores))
        return _format_candidate_rows(
            scored_df,
            target_date,
            model_engine,
            top_k,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            horizon=horizon,
        )

    def _resolve_signal_date(self, tm: datetime.datetime) -> datetime.date:
        """Resolve the latest completed daily bar date visible at ``tm``."""
        cur_date = tm.date()
        if tm.time() <= datetime.time(9, 30):
            if calendar.day_frames is not None:
                return calendar.day_shift(cur_date, -1)
            return cur_date - datetime.timedelta(days=1)
        return cur_date

    def _held_assets(self, as_of: datetime.date | None = None) -> list[str]:
        """返回持仓的证券代码列表。

        Args:
            as_of: 持仓快照日期；给出时优先使用 ``_positions_by_date`` 中该日期
                （含之前最近一个交易日）的持仓，用于归因时还原回测当时的选股池。

        Returns:
            list[str]: 持仓代码（已排序）。
        """
        if as_of is not None and self._positions_by_date:
            latest: datetime.date | None = None
            for day in self._positions_by_date:
                if day <= as_of and (latest is None or day > latest):
                    latest = day
            if latest is not None:
                return sorted(self._positions_by_date[latest])
        positions = getattr(self.broker, "positions", None) or {}
        held: list[str] = []
        for asset, position in positions.items():
            shares = float(getattr(position, "shares", 0) or 0)
            if shares > 0:
                held.append(str(asset))
        return sorted(held)

    def _should_retrain(self, day_key: datetime.date) -> bool:
        """判断是否需要重训模型。

        重训节奏按**交易日**推进：只有当信号日（或最后可用 bar 日）发生变化时
        计数才 +1。回测里每个 bar 对应一个交易日（每 5 个交易日重训一次）；
        实盘里 on_bar 每 2 秒调用一次，但信号日一天只变一次，
        因此实盘同样是每 5 个交易日重训一次。

        Args:
            day_key: 当前信号日（回测路径）或最后可用 bar 日（轻量路径）。

        Returns:
            bool: 是否需要重训。
        """
        if self._last_train_day != day_key:
            self._last_train_day = day_key
            self._days_since_train += 1
        return self._model is None or self._days_since_train >= self.retrain_days

    def _in_decision_window(self, tm: datetime.datetime) -> bool:
        """实盘/模拟盘的决策窗口：交易日 14:57 之后（含盘后补算）。

        回测由 runner 决定触发时刻，不做限制。
        """
        if getattr(self.broker, "_bt_start", None) is not None:
            return True
        try:
            from quantide.service.execution_clock import execution_clock

            phase = execution_clock.phase(tm)
        except Exception:
            return True
        return phase.name in {"auction", "post_close"}

    def _mark_retrained(self) -> None:
        """记录一次重训完成，重置交易日计数。"""
        self._days_since_train = 0

    def _panel_cache_key(self, signal_date: datetime.date) -> tuple[Any, ...]:
        """因子面板的缓存键（必须覆盖所有影响面板内容的要素）。"""
        bt_end = getattr(self.broker, "_bt_end", None)
        is_live = getattr(self.broker, "_bt_start", None) is None
        return (
            signal_date,
            self.train_window,
            self.universe_rebalance_days,
            _coerce_date(bt_end),
            self.universe_size,
            self.universe_mode,
            tuple(self.index_pool_codes),
            round(self.market_cap_trim_pct, 6),
            tuple(self.selected_features or FEATURE_COLS),
            tuple(self._held_assets(signal_date)),
            self.custom_assets,
            _bars_fingerprint(),
            "live" if is_live else "backtest",
        )

    def _panel_end_for(self, signal_date: datetime.date) -> datetime.date:
        """面板窗口结束信号日。"""
        bt_end_attr = getattr(self.broker, "_bt_end", None)
        bt_end_date = _coerce_date(bt_end_attr) or signal_date
        rebalance_end = calendar.shift(
            signal_date, self.universe_rebalance_days, FrameType.DAY
        )
        return max(signal_date, min(rebalance_end, bt_end_date))

    def _load_bars(
        self,
        start: datetime.date,
        end: datetime.date,
        assets: list[str] | None = None,
    ) -> pl.DataFrame:
        """通过 broker 视图取已完成日线；回测走 HistBarView，实盘走 LiveBarView。"""
        broker = self.broker
        if hasattr(broker, "get_bars_range"):
            return broker.get_bars_range(assets, start, end, adjust=None)
        return daily_bars.get_bars_in_range(
            start, end, assets=assets, adjust=None, eager_mode=True
        )

    def _ensure_backtest_panel(self, signal_date: datetime.date) -> None:
        """Pre-fetch and compute causal feature panel covering the backtest window.

        面板按内容键缓存（模块级），参数优化时同一选股池/因子集的面板可跨组合
        复用，避免每个组合重复抓取与计算因子。

        实盘时，LiveBarView 会把今日 forming bar 拼到面板末尾；训练/标签只用
        完成 bar（date < signal_date），forming bar 仅参与当日截面打分，避免
        用盘中价污染前瞻标签。
        """
        if (
            self._cached_norm_panel is not None
            and self._panel_start is not None
            and self._panel_end is not None
            and self._panel_start <= signal_date <= self._panel_end
        ):
            return

        panel_key = self._panel_cache_key(signal_date)
        cached_panel = _PANEL_CACHE.get(panel_key)
        if cached_panel is not None:
            self._cached_norm_panel = cached_panel
            self._panel_start = signal_date
            self._panel_end = self._panel_end_for(signal_date)
            return

        lookback = max(self.train_window + 35, 80)
        fetch_start = calendar.shift(signal_date, -lookback, FrameType.DAY)
        fetch_end = self._panel_end_for(signal_date)

        if self.custom_assets:
            universe_assets: list[str] | None = self.custom_assets
        else:
            # Select liquid universe strictly using history up to signal_date
            init_bars = self._load_bars(
                calendar.shift(signal_date, -25, FrameType.DAY),
                signal_date,
            )
            assert isinstance(init_bars, pl.DataFrame)
            if init_bars.is_empty():
                self._cached_norm_panel = pl.DataFrame()
                return
            init_bars = init_bars.with_columns(pl.col("date").cast(pl.Date))
            latest_init_dt: datetime.date = init_bars["date"].max()  # type: ignore[assignment]
            init_bars = daily_basic.attach(init_bars)
            universe_assets = _filter_universe(
                init_bars,
                latest_init_dt,
                self.universe_size,
                mode=self.universe_mode,
                trim_pct=self.market_cap_trim_pct,
                holdings=self._held_assets(latest_init_dt),
                index_codes=self.index_pool_codes,
            )
            self._live_universe_assets = set(universe_assets)
            self.log(
                f"Universe selected on {latest_init_dt}: "
                f"{len(universe_assets)} assets, mode={self.universe_mode}"
            )
            if not universe_assets:
                self._cached_norm_panel = pl.DataFrame()
                return

        raw_panel = self._load_bars(fetch_start, fetch_end, assets=universe_assets)
        assert isinstance(raw_panel, pl.DataFrame)
        if raw_panel.is_empty():
            self._cached_norm_panel = pl.DataFrame()
            return

        stock_meta = stock_list.data if stock_list.size > 0 else None
        feat_panel = compute_alpha_features(raw_panel, stock_meta=stock_meta)
        self._cached_norm_panel = standardize_features(
            feat_panel, self.selected_features or FEATURE_COLS
        )
        self._panel_start = signal_date
        self._panel_end = fetch_end
        is_live = getattr(self.broker, "_bt_start", None) is None
        if not is_live:
            if len(_PANEL_CACHE) >= _PANEL_CACHE_MAX:
                _PANEL_CACHE.pop(next(iter(_PANEL_CACHE)))
            _PANEL_CACHE[panel_key] = self._cached_norm_panel

    def _training_slice(self, panel: pl.DataFrame, signal_date: datetime.date) -> pl.DataFrame:
        """训练用面板：剔除今日 forming bar，避免用盘中价污染前瞻标签。"""
        if panel.is_empty():
            return panel
        return panel.filter(pl.col("date") < signal_date)

    def _explain_cache_keys(
        self, signal_date: datetime.date, build_signal: datetime.date | None = None
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        """构造归因缓存的键。

        Args:
            signal_date: 决策日。

        Returns:
            tuple: (截面缓存键, 模型缓存键)。
        """
        base = (
            PIPELINE_VERSION,
            FACTOR_VERSION,
            self.train_window,
            self.horizon,
            round(self.stop_loss_pct, 6),
            round(self.take_profit_pct, 6),
            self.universe_size,
            self.universe_mode,
            round(self.market_cap_trim_pct, 6),
            tuple(self.selected_features or FEATURE_COLS),
            tuple(self._held_assets()),
            signal_date,
            build_signal or signal_date,
        )
        return base, base + (self.use_lightgbm,)

    # ---------------- 精确归因：回放回测的选池/训练节奏 ----------------

    def _backtest_bar_dates(
        self, bt_start: datetime.date, bt_end: datetime.date
    ) -> list[datetime.date]:
        """回测逐日推进的 bar 日期序列（交易日）。"""
        try:
            frames = calendar.get_frames(bt_start, bt_end, FrameType.DAY)
        except Exception:
            return []
        return [f for f in frames if isinstance(f, datetime.date)]

    def _replay_panel_builds(
        self, bt_start: datetime.date, bt_end: datetime.date
    ) -> list[tuple[datetime.date, datetime.date]]:
        """回放回测过程中每次重建因子面板的 (构建信号日, 窗口结束信号日)。

        面板按 ``universe_rebalance_days`` 刷新一次，因此选股池在两次刷新之间
        保持不变；归因必须用同一个构建日，才能复现当时的选池。
        """
        builds: list[tuple[datetime.date, datetime.date]] = []
        window_end: datetime.date | None = None
        for bar_date in self._backtest_bar_dates(bt_start, bt_end):
            try:
                signal_date = calendar.day_shift(bar_date, -1)
            except Exception:
                continue
            if window_end is None or signal_date > window_end:
                rebalance_end = calendar.shift(
                    signal_date, self.universe_rebalance_days, FrameType.DAY
                )
                window_end = max(signal_date, min(rebalance_end, bt_end))
                builds.append((signal_date, window_end))
        return builds

    def _replay_context(
        self,
        target_signal: datetime.date,
        bt_start: datetime.date,
        bt_end: datetime.date,
    ) -> tuple[datetime.date, datetime.date, datetime.date]:
        """定位某信号日在回测中对应的面板构建日与最近一次模型重训。

        Args:
            target_signal: 目标信号日（成交日的前一交易日）。
            bt_start: 回测起始交易日。
            bt_end: 回测结束交易日。

        Returns:
            tuple: (目标截面所用面板的构建信号日, 重训所用面板的构建信号日,
            重训的信号日)。
        """
        builds = self._replay_panel_builds(bt_start, bt_end)

        def _build_for(signal_date: datetime.date) -> datetime.date:
            for build_signal, window_end in builds:
                if build_signal <= signal_date <= window_end:
                    return build_signal
            return signal_date

        target_build = _build_for(target_signal)

        bar_dates = self._backtest_bar_dates(bt_start, bt_end)
        refit_signal = target_signal
        refit_build = target_build
        for index, bar_date in enumerate(bar_dates):
            try:
                signal_date = calendar.day_shift(bar_date, -1)
            except Exception:
                continue
            if signal_date > target_signal:
                break
            position = index + 1
            if (position - 1) % max(1, self.retrain_days) == 0:
                refit_signal = signal_date
                refit_build = _build_for(signal_date)
        return target_build, refit_build, refit_signal

    def _explain_cross_section(
        self, signal_date: datetime.date, build_signal: datetime.date | None = None
    ) -> pl.DataFrame:
        """取归因用的目标日截面（只缓存当前这一条）。

        整张因子面板约 160k 行、100MB，不做缓存；这里只缓存目标日截面
        （约 2000 行、1MB），够「同一次交易在特征页与 SHAP 页之间切换」
        不重复计算；换一笔交易则重新计算。

        Args:
            signal_date: 决策日。

        Returns:
            pl.DataFrame: 目标日截面（含原始因子与 z 因子）；可能为空。
        """
        build_signal = build_signal or signal_date
        cross_key, _ = self._explain_cache_keys(signal_date, build_signal)
        cached = _EXPLAIN_CROSS_SECTION_CACHE.get(cross_key)
        if cached is not None:
            return cached
        self._ensure_backtest_panel(build_signal)
        panel = self._cached_norm_panel
        if panel is None or panel.is_empty():
            return pl.DataFrame()
        target_date = panel.filter(pl.col("date") <= pl.lit(signal_date))["date"].max()
        if target_date is None:
            return pl.DataFrame()
        keep = ["date", "asset", "close"] + [
            c for c in (self.selected_features or FEATURE_COLS)
        ] + [f"{c}_z" for c in (self.selected_features or FEATURE_COLS)]
        cross = panel.filter(pl.col("date") == target_date).select(
            [c for c in keep if c in panel.columns]
        )
        _EXPLAIN_CROSS_SECTION_CACHE.clear()
        _EXPLAIN_CROSS_SECTION_CACHE[cross_key] = cross
        return cross

    def _attribution_model(
        self, target_date: datetime.date, build_signal: datetime.date | None = None
    ) -> tuple[Any, str]:
        """取（或训练）归因所需的模型，按决策日缓存。

        模型缓存键含流水线版本、因子版本、训练参数、因子集与当时持仓；
        命中即复用，未命中时临时构建训练面板（不常驻）。

        Args:
            target_date: 截面日。

        Returns:
            tuple: (模型, 引擎名)。
        """
        build_signal = build_signal or target_date
        _, key = self._explain_cache_keys(target_date, build_signal)
        cached = _EXPLAIN_MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        self._ensure_backtest_panel(build_signal)
        panel = self._cached_norm_panel
        if panel is None or panel.is_empty():
            raise RuntimeError("归因面板为空，无法训练模型")
        hist = panel.filter(pl.col("date") <= pl.lit(target_date))
        if hist.is_empty():
            raise RuntimeError("归因训练数据为空")
        model, engine = _fit_ml_model(
            hist,
            train_window=self.train_window,
            horizon=self.horizon,
            use_lightgbm=self.use_lightgbm,
            up_barrier_pct=self.take_profit_pct,
            down_barrier_pct=self.stop_loss_pct,
            feature_cols=self.selected_features or FEATURE_COLS,
            label_cache_key=self._panel_cache_key(build_signal),
        )
        if len(_EXPLAIN_MODEL_CACHE) >= 8:
            _EXPLAIN_MODEL_CACHE.pop(next(iter(_EXPLAIN_MODEL_CACHE)))
        _EXPLAIN_MODEL_CACHE[key] = (model, engine)
        return model, engine

    def explain_features(
        self, asset: str, signal_date: datetime.date
    ) -> list[tuple[str, float]] | None:
        """返回该资产在决策日的因子取值（与模型输入同源）。

        使用策略自己的面板（同一选股池、同一复权口径），因此展示的数值就是
        模型当时看到的值。

        Args:
            asset: 证券代码。
            signal_date: 决策日。

        Returns:
            list | None: (因子名, 取值) 列表；数据不足时返回 ``None``。
        """
        try:
            cross = self._explain_cross_section(signal_date)
        except Exception:
            return None
        if cross is None or cross.is_empty():
            return None
        row = cross.filter(pl.col("asset") == asset)
        if row.is_empty():
            return None
        values = row.row(0, named=True)
        result: list[tuple[str, float]] = []
        for col in FEATURE_COLS:
            value = values.get(col)
            if value is None:
                continue
            try:
                result.append((col, float(value)))
            except (TypeError, ValueError):
                continue
        return result or None

    def explain_prediction(
        self,
        asset: str,
        signal_date: datetime.date,
        bt_start: datetime.date | None = None,
        bt_end: datetime.date | None = None,
    ) -> dict[str, Any] | None:
        """返回该资产在决策日的最终得分归因。

        最终得分就是模型输出（截面 z-score），不含任何手工先验。模型部分用
        LightGBM 自带的 TreeSHAP（``pred_contrib=True``）给出逐特征贡献，
        基线 + 贡献之和可精确还原该股票的截面得分。

        Args:
            asset: 证券代码。
            signal_date: 决策日（信号日，即成交日的前一交易日）。
            bt_start: 回测起始交易日；给出时按回测节奏回放选池与训练，得到与
                当次决策完全一致的截面与模型。
            bt_end: 回测结束交易日。

        Returns:
            dict | None: 归因结果；数据不足或资产不在截面内时返回 ``None``。
        """
        build_signal: datetime.date | None = None
        refit_build: datetime.date | None = None
        refit_signal: datetime.date | None = None
        if bt_start is not None and bt_end is not None:
            try:
                build_signal, refit_build, refit_signal = self._replay_context(
                    signal_date, bt_start, bt_end
                )
            except Exception:
                build_signal = refit_build = refit_signal = None
        try:
            cross = self._explain_cross_section(signal_date, build_signal)
        except Exception:
            return None
        if cross is None or cross.is_empty():
            return None

        target_date = cross["date"][0]
        assets = cross["asset"].to_list()
        if asset not in assets:
            return None
        index = assets.index(asset)

        model, engine = self._attribution_model(
            refit_signal or target_date, refit_build or build_signal
        )
        feature_cols = self.selected_features or FEATURE_COLS
        z_cols = [f"{c}_z" for c in feature_cols]
        x = (
            cross.select(z_cols)
            .fill_null(0.0)
            .fill_nan(0.0)
            .to_numpy()
            .astype(np.float64)
        )
        z_row = x[index]

        contrib = np.zeros(len(feature_cols), dtype=np.float64)
        baseline = 0.0
        if engine == "LightGBM" and hasattr(model, "predict"):
            raw = np.asarray(model.predict(x), dtype=np.float64)
            shap = np.asarray(model.predict(x, pred_contrib=True), dtype=np.float64)
            std_raw = float(np.std(raw)) + 1e-6
            contrib = shap[index][:-1] / std_raw
            baseline = (float(shap[index][-1]) - float(np.mean(raw))) / std_raw
            scores = _zscore_1d(raw)
        elif isinstance(model, np.ndarray):
            ridge_raw = x @ model
            std_ridge = float(np.std(ridge_raw)) + 1e-6
            contrib = (z_row * model) / std_ridge
            baseline = (0.0 - float(np.mean(ridge_raw))) / std_ridge
            scores = _zscore_1d(ridge_raw)
        else:
            return None

        final_score = float(scores[index])
        order = sorted(range(len(assets)), key=lambda i: (-scores[i], assets[i]))
        rank = order.index(index) + 1

        raw_values = cross.row(index, named=True)
        features = []
        for col_index, col in enumerate(feature_cols):
            raw_value = raw_values.get(col)
            features.append(
                {
                    "name": col,
                    "value": None if raw_value is None else float(raw_value),
                    "z": float(z_row[col_index]),
                    "total": float(contrib[col_index]),
                }
            )
        features.sort(key=lambda item: abs(item["total"]), reverse=True)

        return {
            "asset": asset,
            "date": target_date,
            "engine": engine,
            "score": final_score,
            "baseline": float(baseline),
            "rank": int(rank),
            "cross_section": len(assets),
            "features": features,
        }

    def _rank_for_bar(
        self, tm: datetime.datetime
    ) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, int]]:
        """Compute ranked stock candidates, latest close map, and full rank map."""
        use_pull_bars = not getattr(daily_bars, "_initialized", False) or (
            self.custom_assets is not None and len(self.custom_assets) <= 10
        )

        if use_pull_bars:
            # 轻量路径：不走 2000 只成分股的面板，直接按需拉取少量标的的日线，
            # 现场算因子并打分。适用两种情形：
            #   1) 全局行情数据层未就绪（单测 / 独立脚本 / 数据未连接），
            #      此时只能通过 get_bars 取数；
            #   2) 配置显式指定了 <=10 只标的（symbols / universe / symbol），
            #      拉少量标的比构建整面板更省。
            # 注意：本路径与回测路径必须保持同样的重训节奏（按交易日），
            # 否则实盘行为与回测不一致。
            assets = self.custom_assets or [
                str(self.config.get("symbol", "000001.SZ"))
            ]
            frames: list[pl.DataFrame] = []
            lookback = max(self.train_window + 30, 65)
            for asset in assets:
                try:
                    hist = self.get_bars(asset, lookback, tm, "1d")
                except Exception:
                    continue
                if isinstance(hist, pl.DataFrame) and not hist.is_empty():
                    if "asset" not in hist.columns:
                        hist = hist.with_columns(pl.lit(asset).alias("asset"))
                    frames.append(hist)

            if not frames:
                return [], {}, {}

            panel = pl.concat(frames, how="diagonal_relaxed").with_columns(
                pl.col("date").cast(pl.Date)
            )
            if panel.height < 5:
                return [], {}, {}

            feature_cols = self.selected_features or FEATURE_COLS
            feat_df = compute_alpha_features(panel)
            norm_df = standardize_features(feat_df, feature_cols)
            target_date: datetime.date = norm_df["date"].max()  # type: ignore[assignment]
            if self._should_retrain(target_date):
                train_df = norm_df.filter(pl.col("date") < target_date)
                self._model, self._model_engine = _fit_ml_model(
                    train_df,
                    train_window=self.train_window,
                    horizon=self.horizon,
                    use_lightgbm=self.use_lightgbm,
                    up_barrier_pct=self.take_profit_pct,
                    down_barrier_pct=self.stop_loss_pct,
                    feature_cols=feature_cols,
                )
            model, engine = self._model, self._model_engine
            if model is None:
                return [], {}, {}
            cross_df = norm_df.filter(pl.col("date") == target_date)
            scores = _score_cross_section(cross_df, model, engine, feature_cols)
            scored_df = cross_df.with_columns(pl.Series("ml_score", scores)).sort(
                ["ml_score", "asset"], descending=[True, False]
            )
            price_map = {
                str(r["asset"]): float(r["close"])
                for r in scored_df.select(["asset", "close"]).iter_rows(named=True)
            }
            full_ranks = {
                str(a): i for i, a in enumerate(scored_df["asset"].to_list(), start=1)
            }
            ranked = _format_candidate_rows(
                scored_df,
                target_date,
                engine,
                self.keep_top_n,
                take_profit_pct=self.take_profit_pct,
                stop_loss_pct=self.stop_loss_pct,
                horizon=self.horizon,
            )
            return ranked, price_map, full_ranks

        signal_date = self._resolve_signal_date(tm)
        self._ensure_backtest_panel(signal_date)
        if self._cached_norm_panel is None or self._cached_norm_panel.is_empty():
            return [], {}, {}

        hist_slice = self._cached_norm_panel.filter(pl.col("date") <= signal_date)
        if hist_slice.is_empty():
            return [], {}, {}

        target_date = hist_slice["date"].max()  # type: ignore[assignment]
        is_live = getattr(self.broker, "_bt_start", None) is None
        if is_live:
            today = signal_date
            if target_date != today:
                self._data_missing = True
                self.log(
                    f"实盘行情缺失：今日({today}) 无行情，最新 {target_date}，不交易",
                    level="WARNING",
                )
                return [], {}, {}
            today_rows = hist_slice.filter(pl.col("date") == today)
            held = {
                a
                for a, p in self.broker.positions.items()
                if float(getattr(p, "shares", 0) or 0) > 0
            }
            present = set(today_rows["asset"].to_list())
            missing_held = sorted(a for a in held if a not in present)
            if missing_held:
                self._data_missing = True
                self.log(
                    f"实盘行情缺失：持仓 {missing_held} 今日无行情，不交易",
                    level="ERROR",
                )
                return [], {}, {}
            coverage = len(present) / max(1, len(self._live_universe_assets or []))
            if coverage < self.live_min_quote_coverage:
                self._data_missing = True
                self.log(
                    f"实盘行情覆盖不足：{coverage:.1%} < {self.live_min_quote_coverage:.0%}，不交易",
                    level="ERROR",
                )
                return [], {}, {}
        if self._should_retrain(signal_date):
            self._model, self._model_engine = _fit_ml_model(
                self._training_slice(hist_slice, signal_date),
                train_window=self.train_window,
                horizon=self.horizon,
                use_lightgbm=self.use_lightgbm,
                up_barrier_pct=self.take_profit_pct,
                down_barrier_pct=self.stop_loss_pct,
                feature_cols=self.selected_features or FEATURE_COLS,
                label_cache_key=self._panel_cache_key(signal_date),
            )
            self._mark_retrained()

        cross_df = hist_slice.filter(
            (pl.col("date") == target_date)
            & (~pl.col("is_st"))
            & (pl.col("volume") > 0)
            & (pl.col("close") > 0)
        )
        if cross_df.is_empty():
            return [], {}, {}

        scores = _score_cross_section(
            cross_df, self._model, self._model_engine, self.selected_features or FEATURE_COLS
        )
        scored_df = cross_df.with_columns(pl.Series("ml_score", scores)).sort(
            ["ml_score", "asset"], descending=[True, False]
        )
        price_map = {
            str(r["asset"]): float(r["close"])
            for r in scored_df.select(["asset", "close"]).iter_rows(named=True)
        }
        full_ranks = {
            str(a): i for i, a in enumerate(scored_df["asset"].to_list(), start=1)
        }
        ranked = _format_candidate_rows(
            scored_df,
            target_date,
            self._model_engine,
            self.keep_top_n,
            take_profit_pct=self.take_profit_pct,
            stop_loss_pct=self.stop_loss_pct,
            horizon=self.horizon,
        )
        return ranked, price_map, full_ranks

    async def on_bar(self, tm: datetime.datetime) -> None:
        """Execute daily ML ranking, alpha exits, and portfolio rotation.

        实盘/模拟盘每个交易日只决策一次：策略按**信号日**去重，
        避免运行时高频轮询导致重复下单。实盘在 14:57 之后才决策
        （收盘集合竞价）；若 15:00 后仍在运行则继续算（不回退 T+1）。
        """
        self._data_missing = False
        if not self._in_decision_window(tm):
            return
        signal_date = self._resolve_signal_date(tm)
        if self._last_executed_signal_date == signal_date:
            return
        ranked, price_map, full_ranks = self._rank_for_bar(tm)
        if self._data_missing:
            self._data_missing = False
            return
        self._last_executed_signal_date = signal_date

        current_positions = dict(self.broker.positions)

        if not ranked and not current_positions:
            return

        score_map = {item["asset"]: float(item["ml_score"]) for item in ranked}
        is_single_asset = (
            self.custom_assets is not None and len(self.custom_assets) == 1
        ) or (not getattr(daily_bars, "_initialized", False) and len(price_map) <= 1)

        # 1. Alpha exits only. Price/time barriers are owned by the shared
        # triple-barrier risk policy (backtest overlay or live risk strategy).
        # use_alpha_exit=False 时完全关闭 alpha 排名卖出，只保留三重障碍出场。
        for asset, pos in (current_positions.items() if self.use_alpha_exit else ()):
            avail = (
                int(pos.avail)
                if hasattr(pos, "avail")
                else int(getattr(pos, "shares", 0))
            )
            if avail <= 0:
                continue

            cur_rank = full_ranks.get(asset)
            sell_reason: str | None = None
            if is_single_asset:
                if score_map.get(asset, 0.0) < -0.05:
                    sell_reason = (
                        "ML动量反转走弱("
                        f"score={score_map.get(asset, 0.0):.2f})"
                    )
            elif cur_rank is None:
                # 不在选股池内：ST/停牌或被市值裁剪，持仓已不合格
                sell_reason = "已不符合选股池条件(未入池)"
            elif cur_rank > self.keep_top_n:
                sell_reason = f"截面排名跌出前{self.keep_top_n}(排名=#{cur_rank})"

            if sell_reason is not None:
                try:
                    await self.broker.sell(asset, avail, price=0, order_time=tm)
                    self.log(f"[卖出] {asset} 数量={avail} 原因={sell_reason}")
                except Exception as exc:
                    self.log(
                        f"[卖出跳过] {asset} 无法成交: {exc}",
                        level="WARNING",
                    )

        # 2. Buy top-K ranked stocks into available slots
        held_assets = {
            a
            for a, p in self.broker.positions.items()
            if float(getattr(p, "shares", 0) or 0) > 0
        }
        empty_slots = self.top_k - len(held_assets)
        if empty_slots <= 0 or float(self.broker.cash) <= 1000.0:
            return

        min_score = 0.0 if is_single_asset else -0.25
        buy_candidates = [
            item
            for item in ranked[: self.top_k]
            if item["asset"] not in held_assets and item["ml_score"] > min_score
        ][:empty_slots]

        if not buy_candidates:
            return

        alloc_per_slot = (float(self.broker.cash) * 0.98) / empty_slots
        for cand in buy_candidates:
            alloc_cash = min(alloc_per_slot, float(self.broker.cash) * 0.98)
            if alloc_cash <= 500.0:
                continue
            try:
                res = await self.broker.buy_amount(
                    cand["asset"], alloc_cash, price=0, order_time=tm
                )
                if getattr(res, "trades", None) or res is not None:
                    self.log(
                        f"[买入] {cand['asset']}({cand['name']}) "
                        f"排名=#{cand['rank']} ML得分={cand['ml_score']:.3f} "
                        f"形态={cand['kline_pattern']} | {cand['signal_reason']}"
                    )
            except Exception as exc:
                self.log(
                    f"[买入跳过] {cand['asset']} 无法成交: {exc}",
                    level="WARNING",
                )
