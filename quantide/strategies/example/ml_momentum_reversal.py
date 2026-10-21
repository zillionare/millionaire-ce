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

try:
    import lightgbm as lgb

    _HAS_LIGHTGBM = True
except ImportError:  # pragma: no cover
    lgb = None  # type: ignore[assignment]
    _HAS_LIGHTGBM = False

FEATURE_COLS: list[str] = [
    # Group 1: 板块与市场相对强弱
    "rs_board_5d",
    "rs_board_20d",
    "rs_market_rank",
    "downside_resilience",
    # Group 2: 短线 K 线博弈形态
    "upper_shadow",
    "lower_shadow",
    "overnight_gap",
    "intraday_ret",
    "vol_ratio_5_20",
    "high_open_bear",
    "low_open_bull",
    "intraday_vs_overnight_5d",
    # Group 3: 均线支撑与均线斜率
    "ma10_slope",
    "ma20_slope",
    "ma_alignment",
    "bias_5",
    "bias_20",
    "ma20_support_bounce",
    # Group 4: 资金推动与量价特征
    "clv_money_flow",
    "pv_sync_10d",
    "limit_up_gene",
    # Group 5: 动量与反转交叉特征
    "sharpe_mom_20d",
    "rev_5d_shrink_vol",
    "shadow_bias_reversal",
]

PRIOR_WEIGHTS: dict[str, float] = {
    "rs_board_5d": 0.08,
    "rs_board_20d": 0.11,
    "rs_market_rank": 0.05,
    "downside_resilience": 0.06,
    "upper_shadow": -0.05,
    "lower_shadow": 0.04,
    "overnight_gap": -0.02,
    "intraday_ret": 0.05,
    "vol_ratio_5_20": 0.03,
    "high_open_bear": -0.12,
    "low_open_bull": 0.10,
    "intraday_vs_overnight_5d": 0.08,
    "ma10_slope": 0.05,
    "ma20_slope": 0.09,
    "ma_alignment": 0.08,
    "bias_5": -0.03,
    "bias_20": -0.04,
    "ma20_support_bounce": 0.10,
    "clv_money_flow": 0.09,
    "pv_sync_10d": 0.06,
    "limit_up_gene": 0.05,
    "sharpe_mom_20d": 0.10,
    "rev_5d_shrink_vol": 0.08,
    "shadow_bias_reversal": 0.09,
}


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


def compute_alpha_features(
    df: pl.DataFrame,
    stock_meta: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Compute 5 groups of momentum, reversal, and microstructure features.

    Args:
        df: Daily bar DataFrame containing at least
            ``[date, asset, open, high, low, close, volume]``.
        stock_meta: Optional metadata DataFrame with ``[asset, industry]``.

    Returns:
        Polars DataFrame sorted by ``[asset, date]`` with all feature columns.
    """
    if df.is_empty():
        return df

    cols = set(df.columns)
    norm_exprs: list[pl.Expr] = [
        pl.col("date").cast(pl.Date).alias("date"),
        pl.col("asset").cast(pl.Utf8).alias("asset"),
        pl.col("open").cast(pl.Float64).alias("open"),
        pl.col("high").cast(pl.Float64).alias("high"),
        pl.col("low").cast(pl.Float64).alias("low"),
        pl.col("close").cast(pl.Float64).alias("close"),
        pl.col("volume").cast(pl.Float64).fill_null(0.0).alias("volume"),
    ]
    if "amount" in cols:
        norm_exprs.append(
            pl.when(pl.col("amount").is_not_null() & (pl.col("amount") > 0))
            .then(pl.col("amount").cast(pl.Float64))
            .otherwise(
                pl.col("volume").cast(pl.Float64) * pl.col("close").cast(pl.Float64)
            )
            .alias("amount")
        )
    else:
        norm_exprs.append(
            (
                pl.col("volume").cast(pl.Float64) * pl.col("close").cast(pl.Float64)
            ).alias("amount")
        )

    if "adjust" in cols:
        norm_exprs.append(
            pl.col("adjust").cast(pl.Float64).fill_null(1.0).alias("adjust")
        )
    else:
        norm_exprs.append(pl.lit(1.0).alias("adjust"))

    if "is_st" in cols:
        norm_exprs.append(
            pl.col("is_st").cast(pl.Boolean).fill_null(False).alias("is_st")
        )
    else:
        norm_exprs.append(pl.lit(False).alias("is_st"))

    if "up_limit" in cols:
        norm_exprs.append(
            pl.col("up_limit").cast(pl.Float64).fill_null(0.0).alias("up_limit")
        )
    else:
        norm_exprs.append(pl.lit(0.0).alias("up_limit"))

    if "down_limit" in cols:
        norm_exprs.append(
            pl.col("down_limit").cast(pl.Float64).fill_null(0.0).alias("down_limit")
        )
    else:
        norm_exprs.append(pl.lit(0.0).alias("down_limit"))

    out = df.with_columns(norm_exprs).sort(["asset", "date"])

    prefix_board = (
        pl.when(pl.col("asset").str.starts_with("688"))
        .then(pl.lit("科创板"))
        .when(
            pl.col("asset").str.starts_with("300")
            | pl.col("asset").str.starts_with("301")
            | pl.col("asset").str.starts_with("302")
        )
        .then(pl.lit("创业板"))
        .when(pl.col("asset").str.starts_with("002"))
        .then(pl.lit("深市中小"))
        .when(
            pl.col("asset").str.starts_with("60")
            | pl.col("asset").str.starts_with("00")
        )
        .then(pl.lit("主板"))
        .otherwise(pl.lit("其他"))
    )

    if (
        stock_meta is not None
        and not stock_meta.is_empty()
        and "asset" in stock_meta.columns
        and "industry" in stock_meta.columns
        and stock_meta["industry"].drop_nulls().len() > 0
    ):
        meta_clean = (
            stock_meta.select(
                [
                    pl.col("asset").cast(pl.Utf8),
                    pl.col("industry").cast(pl.Utf8).alias("_meta_board"),
                ]
            )
            .unique(subset=["asset"], keep="first")
        )
        out = (
            out.join(meta_clean, on="asset", how="left")
            .with_columns(
                pl.when(
                    pl.col("_meta_board").is_not_null()
                    & (pl.col("_meta_board").str.len_chars() > 0)
                )
                .then(pl.col("_meta_board"))
                .otherwise(prefix_board)
                .alias("board")
            )
            .drop("_meta_board")
        )
    else:
        out = out.with_columns(prefix_board.alias("board"))

    # Basic returns & moving averages per asset
    out = out.with_columns(
        [
            (pl.col("close") / pl.col("close").shift(1).over("asset") - 1.0).alias(
                "ret_1d"
            ),
            (pl.col("close") / pl.col("close").shift(3).over("asset") - 1.0).alias(
                "ret_3d"
            ),
            (pl.col("close") / pl.col("close").shift(5).over("asset") - 1.0).alias(
                "ret_5d"
            ),
            (pl.col("close") / pl.col("close").shift(10).over("asset") - 1.0).alias(
                "ret_10d"
            ),
            (pl.col("close") / pl.col("close").shift(20).over("asset") - 1.0).alias(
                "ret_20d"
            ),
            pl.col("close")
            .rolling_mean(window_size=5, min_samples=2)
            .over("asset")
            .alias("ma5"),
            pl.col("close")
            .rolling_mean(window_size=10, min_samples=2)
            .over("asset")
            .alias("ma10"),
            pl.col("close")
            .rolling_mean(window_size=20, min_samples=2)
            .over("asset")
            .alias("ma20"),
            pl.col("close")
            .rolling_mean(window_size=60, min_samples=2)
            .over("asset")
            .alias("ma60"),
            pl.col("amount")
            .rolling_mean(window_size=5, min_samples=2)
            .over("asset")
            .alias("vol_ma5"),
            pl.col("amount")
            .rolling_mean(window_size=20, min_samples=2)
            .over("asset")
            .alias("vol_ma20"),
            (pl.col("high") - pl.col("low")).clip(lower_bound=1e-6).alias("bar_range"),
            pl.max_horizontal("open", "close").alias("body_top"),
            pl.min_horizontal("open", "close").alias("body_bot"),
            (pl.col("open") / pl.col("close").shift(1).over("asset") - 1.0).alias(
                "overnight_gap"
            ),
            (pl.col("close") / pl.col("open").clip(lower_bound=1e-6) - 1.0).alias(
                "intraday_ret"
            ),
        ]
    )

    # Intermediate cross-sectional & candlestick columns
    out = out.with_columns(
        [
            pl.col("ret_5d").mean().over(["date", "board"]).alias("board_ret_5d"),
            pl.col("ret_20d").mean().over(["date", "board"]).alias("board_ret_20d"),
            pl.col("ret_1d").mean().over("date").alias("mkt_ret_1d"),
            ((pl.col("high") - pl.col("body_top")) / pl.col("bar_range")).alias(
                "upper_shadow"
            ),
            ((pl.col("body_bot") - pl.col("low")) / pl.col("bar_range")).alias(
                "lower_shadow"
            ),
            (pl.col("vol_ma5") / (pl.col("vol_ma20") + 1e-6)).alias("vol_ratio_5_20"),
            (pl.col("ma10") / pl.col("ma10").shift(5).over("asset") - 1.0).alias(
                "ma10_slope"
            ),
            (pl.col("ma20") / pl.col("ma20").shift(5).over("asset") - 1.0).alias(
                "ma20_slope"
            ),
            (
                (
                    (pl.col("close") > pl.col("ma5")).cast(pl.Float64)
                    + (pl.col("ma5") > pl.col("ma10")).cast(pl.Float64)
                    + (pl.col("ma10") > pl.col("ma20")).cast(pl.Float64)
                )
                / 3.0
            ).alias("ma_alignment"),
            (pl.col("close") / (pl.col("ma5") + 1e-6) - 1.0).alias("bias_5"),
            (pl.col("close") / (pl.col("ma20") + 1e-6) - 1.0).alias("bias_20"),
            (
                (2.0 * pl.col("close") - pl.col("high") - pl.col("low"))
                / pl.col("bar_range")
            ).alias("clv"),
            pl.col("ret_1d")
            .rolling_std(window_size=20, min_samples=3)
            .over("asset")
            .fill_null(0.02)
            .clip(lower_bound=0.005)
            .alias("volatility_20d"),
        ]
    )

    # All 5 feature groups
    out = out.with_columns(
        [
            # Group 1: Relative Strength
            (pl.col("ret_5d") - pl.col("board_ret_5d")).alias("rs_board_5d"),
            (pl.col("ret_20d") - pl.col("board_ret_20d")).alias("rs_board_20d"),
            (
                pl.col("ret_10d").rank().over("date")
                / pl.len().over("date").clip(lower_bound=1)
            ).alias("rs_market_rank"),
            (
                (pl.col("ret_1d") - pl.col("mkt_ret_1d"))
                * pl.when(pl.col("mkt_ret_1d") < 0).then(1.0).otherwise(0.3)
            )
            .rolling_mean(window_size=10, min_samples=1)
            .over("asset")
            .alias("downside_resilience"),
            # Group 2: Short-term Candlestick Patterns
            (
                pl.max_horizontal(pl.col("overnight_gap"), pl.lit(0.0))
                * pl.max_horizontal(-pl.col("intraday_ret"), pl.lit(0.0))
                * pl.col("vol_ratio_5_20")
            ).alias("high_open_bear"),
            (
                pl.max_horizontal(-pl.col("overnight_gap"), pl.lit(0.0))
                * pl.max_horizontal(pl.col("intraday_ret"), pl.lit(0.0))
                * pl.col("vol_ratio_5_20")
            ).alias("low_open_bull"),
            (pl.col("intraday_ret") - pl.col("overnight_gap"))
            .rolling_sum(window_size=5, min_samples=1)
            .over("asset")
            .alias("intraday_vs_overnight_5d"),
            # Group 3: MA Slope & Support
            pl.when(
                (pl.col("ma20_slope") > 0)
                & (pl.col("low") <= pl.col("ma20") * 1.02)
                & (pl.col("close") >= pl.col("ma20") * 0.995)
            )
            .then(
                pl.col("lower_shadow")
                + pl.max_horizontal(pl.col("intraday_ret") * 10.0, pl.lit(0.0))
                + 0.5
            )
            .otherwise(0.0)
            .alias("ma20_support_bounce"),
            # Group 4: Money Flow & Volume-Price
            (pl.col("clv") * pl.col("vol_ratio_5_20"))
            .rolling_mean(window_size=5, min_samples=1)
            .over("asset")
            .alias("clv_money_flow"),
            (
                pl.col("ret_1d").sign()
                * (pl.col("amount") / (pl.col("vol_ma20") + 1e-6) - 1.0)
            )
            .rolling_mean(window_size=10, min_samples=1)
            .over("asset")
            .alias("pv_sync_10d"),
            (
                ((pl.col("up_limit") > 0) & (pl.col("close") >= pl.col("up_limit") * 0.992))
                | (pl.col("ret_1d") >= 0.095)
            )
            .cast(pl.Float64)
            .rolling_sum(window_size=20, min_samples=1)
            .over("asset")
            .alias("limit_up_gene"),
            # Group 5: Momentum-Reversal Interactions
            (
                pl.col("ret_20d") / (pl.col("volatility_20d") * (20.0**0.5))
            ).alias("sharpe_mom_20d"),
            (-pl.col("ret_5d") / (pl.col("vol_ratio_5_20") + 0.5)).alias(
                "rev_5d_shrink_vol"
            ),
            (
                -pl.col("upper_shadow")
                * pl.max_horizontal(pl.col("bias_20"), pl.lit(0.0))
                * pl.col("vol_ratio_5_20")
                + pl.col("lower_shadow")
                * pl.max_horizontal(-pl.col("bias_5"), pl.lit(0.0))
            ).alias("shadow_bias_reversal"),
        ]
    )

    return out


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


def _standardize_features(
    df: pl.DataFrame,
    feature_cols: list[str] | None = None,
) -> pl.DataFrame:
    """Standardize feature columns via winsorized cross-sectional Z-score per date.

    Falls back to rolling time-series standardization when a panel only contains
    a single asset per date.
    """
    if df.is_empty():
        return df

    cols = feature_cols or FEATURE_COLS
    n_assets = df["asset"].n_unique()

    z_exprs: list[pl.Expr] = []
    if n_assets > 1:
        for col in cols:
            raw = pl.col(col).cast(pl.Float64).fill_nan(None)
            mean = raw.mean().over("date")
            std = raw.std().over("date").fill_null(0.0)
            clipped = raw.clip(
                lower_bound=mean - 3.0 * std,
                upper_bound=mean + 3.0 * std,
            )
            z = ((clipped - mean) / (std + 1e-6)).fill_null(0.0).fill_nan(0.0)
            z_exprs.append(z.alias(f"{col}_z"))
    else:
        for col in cols:
            raw = pl.col(col).cast(pl.Float64).fill_nan(None)
            mean = raw.rolling_mean(window_size=20, min_samples=2).over("asset")
            std = (
                raw.rolling_std(window_size=20, min_samples=2)
                .over("asset")
                .fill_null(0.0)
            )
            clipped = raw.clip(
                lower_bound=mean - 3.0 * std,
                upper_bound=mean + 3.0 * std,
            )
            z = ((clipped - mean) / (std + 1e-6)).fill_null(0.0).fill_nan(0.0)
            z_exprs.append(z.alias(f"{col}_z"))

    return df.with_columns(z_exprs)


def _zscore_1d(arr: np.ndarray) -> np.ndarray:
    """Normalize a 1D numpy array to zero mean and unit variance."""
    clean = np.nan_to_num(arr.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    if clean.size <= 1:
        return np.clip(clean, -3.0, 3.0)
    std = float(np.std(clean))
    if std < 1e-8:
        return np.zeros_like(clean)
    return (clean - float(np.mean(clean))) / (std + 1e-6)


def _compute_prior_score(
    x_norm: pl.DataFrame | np.ndarray,
    feature_cols: list[str] | None = None,
) -> np.ndarray:
    """Compute domain-knowledge composite prior score from normalized features."""
    cols = feature_cols or FEATURE_COLS
    if isinstance(x_norm, pl.DataFrame):
        use_z = all(f"{c}_z" in x_norm.columns for c in cols)
        col_names = [f"{c}_z" if use_z else c for c in cols]
        mat = (
            x_norm.select(col_names)
            .fill_null(0.0)
            .fill_nan(0.0)
            .to_numpy()
            .astype(np.float64)
        )
    else:
        mat = np.nan_to_num(x_norm.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)

    weights = np.array([PRIOR_WEIGHTS.get(c, 0.0) for c in cols], dtype=np.float64)
    score = mat @ weights

    if "bias_20" in cols and "upper_shadow" in cols:
        idx_bias = cols.index("bias_20")
        idx_up = cols.index("upper_shadow")
        overextended_penalty = np.maximum(mat[:, idx_bias], 0.0) * np.maximum(
            mat[:, idx_up], 0.0
        )
        score = score - 0.08 * overextended_penalty

    return np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)


def _fit_ml_model(
    hist_df: pl.DataFrame,
    train_window: int = 45,
    horizon: int = 3,
    use_lightgbm: bool = True,
    up_barrier_pct: float = 0.15,
    down_barrier_pct: float = 0.06,
) -> tuple[Any, str]:
    """Train LightGBM or Ridge fallback strictly on leak-free historical samples.

    Labels are triple-barrier outcomes computed from ``hist_df``. The caller
    MUST still filter ``date <= signal_date`` so shifted forward bars cannot
    observe future prices.
    """
    z_cols = [f"{c}_z" for c in FEATURE_COLS]
    labeled = compute_triple_barrier_labels(
        hist_df,
        horizon=horizon,
        up_barrier_pct=up_barrier_pct,
        down_barrier_pct=down_barrier_pct,
    )
    valid_train = labeled.filter(
        pl.col("tb_ret").is_not_null() & pl.col("tb_ret").is_finite()
    )
    if valid_train.is_empty():
        return None, "Prior-Ridge"

    train_dates = valid_train["date"].unique().sort().tail(max(5, train_window))
    valid_train = valid_train.filter(pl.col("date").is_in(train_dates.to_list()))
    if valid_train.is_empty():
        return None, "Prior-Ridge"

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
) -> np.ndarray:
    """Predict blended ML + domain prior scores for a target date cross-section."""
    if pred_df.is_empty():
        return np.array([], dtype=np.float64)

    z_cols = [f"{c}_z" for c in FEATURE_COLS]
    x_pred = (
        pred_df.select(z_cols)
        .fill_null(0.0)
        .fill_nan(0.0)
        .to_numpy()
        .astype(np.float64)
    )
    prior_raw = _compute_prior_score(x_pred, FEATURE_COLS)
    prior_z = _zscore_1d(prior_raw) if len(x_pred) > 1 else prior_raw

    if model is None:
        return prior_z

    if model_engine == "LightGBM" and hasattr(model, "predict"):
        raw_pred = np.asarray(model.predict(x_pred), dtype=np.float64)
        lgbm_z = _zscore_1d(raw_pred) if len(x_pred) > 1 else raw_pred
        return 0.7 * lgbm_z + 0.3 * prior_z

    if isinstance(model, np.ndarray):
        ridge_pred = x_pred @ model
        ridge_z = _zscore_1d(ridge_pred) if len(x_pred) > 1 else ridge_pred
        return 0.5 * ridge_z + 0.5 * prior_z

    return prior_z


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


def _filter_universe(
    bars_df: pl.DataFrame,
    target_date: datetime.date,
    universe_size: int,
    mode: str = "market_cap_trim",
    trim_pct: float = 0.05,
) -> list[str]:
    """Select the tradable universe on ``target_date``.

    ``market_cap_trim`` first removes the largest and smallest ``trim_pct`` of
    the cross-section by ``total_mv``, then removes suspended and ST stocks
    from the remaining set.  ``liquidity`` keeps the legacy top-N by 20-day
    mean amount.
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
            return sorted(cap["asset"].unique().to_list())

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
    PARAMS: dict[str, Any] = {
        "top_k": 5,
        "keep_top_n": 15,
        "universe_size": 300,
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
            self.keep_top_n, int(self.config.get("universe_size", 300))
        )
        self.universe_mode = str(
            self.config.get("universe_mode", "market_cap_trim")
        ).strip()
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
        self.custom_assets = _parse_custom_assets(self.config)

        self._model: Any = None
        self._model_engine: str = "LightGBM" if self.use_lightgbm else "Prior-Ridge"
        self._bars_since_train: int = 0
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
            f"use_lightgbm={self.use_lightgbm}"
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
        universe_size = max(top_k, int(cfg.get("universe_size", 300)))
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
                adjust="qfq",
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
            )
        if not candidates:
            return []

        panel = panel.filter(pl.col("asset").is_in(candidates))
        stock_meta = stock_list.data if stock_list.size > 0 else None
        feat_df = compute_alpha_features(panel, stock_meta=stock_meta)
        norm_df = _standardize_features(feat_df, FEATURE_COLS)

        model, model_engine = _fit_ml_model(
            norm_df,
            train_window=train_window,
            horizon=horizon,
            use_lightgbm=use_lightgbm,
            up_barrier_pct=take_profit_pct,
            down_barrier_pct=stop_loss_pct,
        )
        cross_df = norm_df.filter(pl.col("date") == target_date)
        if cross_df.is_empty():
            return []

        scores = _score_cross_section(cross_df, model, model_engine)
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

    def _ensure_backtest_panel(self, signal_date: datetime.date) -> None:
        """Pre-fetch and compute causal feature panel covering the backtest window."""
        if (
            self._cached_norm_panel is not None
            and self._panel_start is not None
            and self._panel_end is not None
            and self._panel_start <= signal_date <= self._panel_end
        ):
            return

        lookback = max(self.train_window + 35, 80)
        fetch_start = calendar.shift(signal_date, -lookback, FrameType.DAY)
        bt_end_attr = getattr(self.broker, "_bt_end", None)
        bt_end_date = _coerce_date(bt_end_attr) or signal_date
        rebalance_end = calendar.shift(
            signal_date, self.universe_rebalance_days, FrameType.DAY
        )
        fetch_end = max(signal_date, min(rebalance_end, bt_end_date))

        if self.custom_assets:
            universe_assets: list[str] | None = self.custom_assets
        else:
            # Select liquid universe strictly using history up to signal_date
            init_bars = daily_bars.get_bars_in_range(
                calendar.shift(signal_date, -25, FrameType.DAY),
                signal_date,
                adjust="qfq",
                eager_mode=True,
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
            )
            self.log(
                f"Universe selected on {latest_init_dt}: "
                f"{len(universe_assets)} assets, mode={self.universe_mode}"
            )
            if not universe_assets:
                self._cached_norm_panel = pl.DataFrame()
                return

        raw_panel = daily_bars.get_bars_in_range(
            fetch_start,
            fetch_end,
            assets=universe_assets,
            adjust="qfq",
            eager_mode=True,
        )
        assert isinstance(raw_panel, pl.DataFrame)
        if raw_panel.is_empty():
            self._cached_norm_panel = pl.DataFrame()
            return

        stock_meta = stock_list.data if stock_list.size > 0 else None
        feat_panel = compute_alpha_features(raw_panel, stock_meta=stock_meta)
        self._cached_norm_panel = _standardize_features(feat_panel, FEATURE_COLS)
        self._panel_start = signal_date
        self._panel_end = fetch_end

    def _rank_for_bar(
        self, tm: datetime.datetime
    ) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, int]]:
        """Compute ranked stock candidates, latest close map, and full rank map."""
        use_pull_bars = not getattr(daily_bars, "_initialized", False) or (
            self.custom_assets is not None and len(self.custom_assets) <= 10
        )

        if use_pull_bars:
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

            feat_df = compute_alpha_features(panel)
            norm_df = _standardize_features(feat_df, FEATURE_COLS)
            target_date: datetime.date = norm_df["date"].max()  # type: ignore[assignment]
            model, engine = _fit_ml_model(
                norm_df,
                train_window=self.train_window,
                horizon=self.horizon,
                use_lightgbm=self.use_lightgbm,
                up_barrier_pct=self.take_profit_pct,
                down_barrier_pct=self.stop_loss_pct,
            )
            cross_df = norm_df.filter(pl.col("date") == target_date)
            scores = _score_cross_section(cross_df, model, engine)
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
        if self._model is None or self._bars_since_train >= self.retrain_days:
            self._model, self._model_engine = _fit_ml_model(
                hist_slice,
                train_window=self.train_window,
                horizon=self.horizon,
                use_lightgbm=self.use_lightgbm,
                up_barrier_pct=self.take_profit_pct,
                down_barrier_pct=self.stop_loss_pct,
            )
            self._bars_since_train = 0
        self._bars_since_train += 1

        cross_df = hist_slice.filter(
            (pl.col("date") == target_date)
            & (~pl.col("is_st"))
            & (pl.col("volume") > 0)
            & (pl.col("close") > 0)
        )
        if cross_df.is_empty():
            return [], {}, {}

        scores = _score_cross_section(cross_df, self._model, self._model_engine)
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
        """Execute daily ML ranking, alpha exits, and portfolio rotation."""
        ranked, price_map, full_ranks = self._rank_for_bar(tm)

        current_positions = dict(self.broker.positions)

        if not ranked and not current_positions:
            return

        score_map = {item["asset"]: float(item["ml_score"]) for item in ranked}
        is_single_asset = (
            self.custom_assets is not None and len(self.custom_assets) == 1
        ) or (not getattr(daily_bars, "_initialized", False) and len(price_map) <= 1)

        # 1. Alpha exits only. Price/time barriers are owned by the shared
        # triple-barrier risk policy (backtest overlay or live risk strategy).
        for asset, pos in current_positions.items():
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
            elif cur_rank is None or cur_rank > self.keep_top_n:
                rank_label = f"#{cur_rank}" if cur_rank is not None else "未入池"
                sell_reason = (
                    f"截面排名跌出前{self.keep_top_n}(排名={rank_label})"
                )

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
