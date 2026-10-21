"""Unit tests for the ML momentum/reversal stock-selection strategy.

Coverage targets:
- Strategy discovery/metadata and parameter contract.
- The five alpha-feature groups and industry-based board override.
- Triple-barrier labeling including same-day dual-hit resolution.
- LightGBM / Prior-Ridge selection, exclusions, and barrier metadata.
- ``on_bar`` buy/sell ordering, T+1, stop-loss, take-profit, and time exit.
- Web stock-selection modal, list button, and POST re-run route.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fasthtml.common import to_xml

from quantide.core.strategy import BaseStrategy
from quantide.core.strategy_discovery import StrategyDiscovery
from quantide.strategies.example import ml_momentum_reversal
from quantide.strategies.example.ml_momentum_reversal import (
    MLMomentumReversalStrategy,
    compute_triple_barrier_labels,
)
from quantide.web.pages import strategy as strategy_page
from tests.unit.quantide.factors.panel_builder import _make_synthetic_panel

try:
    import lightgbm as _lightgbm  # noqa: F401

    HAS_LIGHTGBM = True
except ImportError:  # pragma: no cover - exercised only without the optional dep
    HAS_LIGHTGBM = False


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------


def _barrier_panel(rows: list[tuple[float, float, float, float]]) -> pl.DataFrame:
    """Build a single-asset OHLC panel from ``(open, high, low, close)`` rows."""
    start = dt.date(2024, 1, 2)
    return pl.DataFrame(
        {
            "date": [start + dt.timedelta(days=i) for i in range(len(rows))],
            "asset": ["600001.SH"] * len(rows),
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
        }
    )


def _lock_up_limit(panel: pl.DataFrame, asset: str) -> pl.DataFrame:
    """Make ``asset`` close at its up-limit on the final bar."""
    last_date = panel["date"].max()
    mask = (pl.col("asset") == asset) & (pl.col("date") == last_date)
    limit = panel.filter(mask)["up_limit"][0]
    return panel.with_columns(
        pl.when(mask).then(pl.lit(limit)).otherwise(pl.col("close")).alias("close"),
        pl.when(mask)
        .then(pl.max_horizontal(pl.col("high"), pl.lit(limit)))
        .otherwise(pl.col("high"))
        .alias("high"),
    )


# ---------------------------------------------------------------------------
# 1. Discovery & metadata contract
# ---------------------------------------------------------------------------


def test_strategy_is_a_base_strategy_subclass() -> None:
    assert issubclass(MLMomentumReversalStrategy, BaseStrategy)


def test_strategy_discovery_finds_clean_metadata() -> None:
    root = Path(ml_momentum_reversal.__file__).parent
    result = StrategyDiscovery.discover(root)

    matches = [s for s in result.strategies if s.name == "MLMomentumReversalStrategy"]
    assert len(matches) == 1
    metadata = matches[0]
    assert metadata.strategy_type == "independent"
    assert metadata.default_config == MLMomentumReversalStrategy.default_config()

    not_a_strategy = [
        d for d in result.diagnostics if d.reason.value == "NotAStrategy"
    ]
    invalid_config = [
        d for d in result.diagnostics if d.reason.value == "InvalidConfig"
    ]
    assert not_a_strategy == []
    assert invalid_config == []
    assert result.diagnostics == []


def test_default_config_and_params_expose_required_keys() -> None:
    required = {
        "top_k",
        "keep_top_n",
        "universe_size",
        "train_window",
        "horizon",
        "retrain_days",
        "stop_loss_pct",
        "take_profit_pct",
        "use_lightgbm",
    }
    assert required <= set(MLMomentumReversalStrategy.PARAMS)
    assert required <= set(MLMomentumReversalStrategy.default_config())
    assert MLMomentumReversalStrategy.default_config() == (
        MLMomentumReversalStrategy.PARAMS
    )
    assert MLMomentumReversalStrategy.default_config() is not (
        MLMomentumReversalStrategy.PARAMS
    )


# ---------------------------------------------------------------------------
# 2. Feature engineering (5 groups)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. Triple barrier labeling
# ---------------------------------------------------------------------------


def test_triple_barrier_upper_hit_returns_positive_barrier() -> None:
    rows = [
        (100.0, 101.0, 99.0, 100.0),
        (101.0, 116.0, 100.0, 112.0),
        (110.0, 113.0, 108.0, 111.0),
        (111.0, 113.0, 109.0, 112.0),
        (112.0, 114.0, 110.0, 113.0),
        (113.0, 115.0, 111.0, 114.0),
    ]
    labeled = compute_triple_barrier_labels(_barrier_panel(rows), horizon=3)

    first = labeled.row(0, named=True)
    assert first["tb_hit"] == "up"
    assert first["tb_ret"] == pytest.approx(0.15)
    assert first["tb_hold_days"] == 1
    assert first["fwd_ret"] == pytest.approx(0.15)


def test_triple_barrier_lower_hit_returns_negative_barrier() -> None:
    rows = [
        (100.0, 101.0, 99.0, 100.0),
        (101.0, 102.0, 93.0, 95.0),
        (95.0, 97.0, 94.0, 96.0),
        (96.0, 98.0, 95.0, 97.0),
        (97.0, 99.0, 96.0, 98.0),
        (98.0, 100.0, 97.0, 99.0),
    ]
    labeled = compute_triple_barrier_labels(_barrier_panel(rows), horizon=3)

    first = labeled.row(0, named=True)
    assert first["tb_hit"] == "down"
    assert first["tb_ret"] == pytest.approx(-0.06)
    assert first["tb_hold_days"] == 1


@pytest.mark.parametrize(
    ("open_price", "expected_hit", "expected_ret"),
    [(114.0, "up", 0.15), (95.0, "down", -0.06)],
)
def test_same_day_dual_hit_resolves_by_open_proximity(
    open_price: float,
    expected_hit: str,
    expected_ret: float,
) -> None:
    rows = [
        (100.0, 101.0, 99.0, 100.0),
        (open_price, 116.0, 93.0, 100.0),
        (100.0, 102.0, 99.0, 101.0),
        (101.0, 103.0, 100.0, 102.0),
        (102.0, 104.0, 101.0, 103.0),
        (103.0, 105.0, 102.0, 104.0),
    ]
    labeled = compute_triple_barrier_labels(_barrier_panel(rows), horizon=3)

    first = labeled.row(0, named=True)
    assert first["tb_hit"] == expected_hit
    assert first["tb_ret"] == pytest.approx(expected_ret)


def test_same_day_dual_hit_tie_resolves_down() -> None:
    # Use exactly representable binary fractions so the open is equidistant
    # from both barriers: upper 112.5, lower 93.75, midpoint 103.125.
    rows = [
        (100.0, 101.0, 99.0, 100.0),
        (103.125, 113.0, 93.0, 100.0),
        (100.0, 102.0, 99.0, 101.0),
        (101.0, 103.0, 100.0, 102.0),
        (102.0, 104.0, 101.0, 103.0),
        (103.0, 105.0, 102.0, 104.0),
    ]
    labeled = compute_triple_barrier_labels(
        _barrier_panel(rows),
        horizon=3,
        up_barrier_pct=0.125,
        down_barrier_pct=0.0625,
    )

    first = labeled.row(0, named=True)
    assert first["tb_hit"] == "down"
    assert first["tb_ret"] == pytest.approx(-0.0625)


def test_triple_barrier_time_expiry_uses_forward_close() -> None:
    rows = [
        (100.0, 101.0, 99.0, 100.0),
        (100.0, 101.0, 99.0, 101.0),
        (101.0, 102.0, 100.0, 102.0),
        (102.0, 103.0, 101.0, 103.0),
        (103.0, 104.0, 102.0, 104.0),
        (104.0, 105.0, 103.0, 105.0),
    ]
    labeled = compute_triple_barrier_labels(_barrier_panel(rows), horizon=3)

    first = labeled.row(0, named=True)
    assert first["tb_hit"] == "expire"
    assert first["tb_ret"] == pytest.approx(103.0 / 100.0 - 1.0)
    assert first["tb_hold_days"] == 3


def test_triple_barrier_has_no_lookahead_on_last_horizon_rows() -> None:
    horizon = 3
    labeled = compute_triple_barrier_labels(
        _make_synthetic_panel(), horizon=horizon
    )

    null_counts = labeled.group_by("asset").agg(
        pl.col("tb_ret").is_null().sum().alias("null_count")
    )
    assert null_counts["null_count"].to_list() == [horizon] * labeled[
        "asset"
    ].n_unique()

    non_null = labeled.filter(pl.col("tb_ret").is_not_null())
    hold_days = non_null["tb_hold_days"]
    assert hold_days.min() >= 1
    assert hold_days.max() <= horizon
    assert non_null["tb_hit"].is_in(["up", "down", "expire"]).all()


# ---------------------------------------------------------------------------
# 4. LightGBM & Prior-Ridge stock selection
# ---------------------------------------------------------------------------


_SELECTION_CONFIG = {
    "top_k": 5,
    "keep_top_n": 15,
    "universe_size": 1000,
    "train_window": 45,
    "horizon": 3,
    "retrain_days": 5,
    "stop_loss_pct": 0.06,
    "take_profit_pct": 0.15,
    "use_lightgbm": True,
}

_CANDIDATE_KEYS = {
    "rank",
    "asset",
    "name",
    "board",
    "close",
    "ml_score",
    "kline_pattern",
    "signal_reason",
    "tp_price",
    "sl_price",
    "barrier_horizon",
    "tb_barrier_text",
}


def test_select_stocks_lightgbm_contract_and_barrier_metadata() -> None:
    panel = _make_synthetic_panel()
    picks = MLMomentumReversalStrategy.select_stocks(
        bars_df=panel, config=_SELECTION_CONFIG
    )

    assert len(picks) == _SELECTION_CONFIG["top_k"]
    assert [item["rank"] for item in picks] == list(range(1, len(picks) + 1))

    # 65 days x 12 assets leaves ~45 training dates x 11 eligible assets,
    # far above the 30-sample LightGBM threshold in ``_fit_ml_model``.
    if HAS_LIGHTGBM:
        assert {item["model_engine"] for item in picks} == {"LightGBM"}
    else:  # pragma: no cover - only when the optional dependency is missing
        assert {item["model_engine"] for item in picks} <= {
            "LightGBM",
            "Prior-Ridge",
        }

    for item in picks:
        assert _CANDIDATE_KEYS <= set(item)
        assert item["barrier_horizon"] == 3
        assert item["tp_price"] == pytest.approx(
            round(item["close"] * 1.15, 2)
        )
        assert item["sl_price"] == pytest.approx(
            round(item["close"] * 0.94, 2)
        )
        assert "+15%" in item["tb_barrier_text"]
        assert "-6%" in item["tb_barrier_text"]
        assert "3日" in item["tb_barrier_text"]


def test_select_stocks_prior_ridge_when_lightgbm_disabled() -> None:
    panel = _make_synthetic_panel()
    picks = MLMomentumReversalStrategy.select_stocks(
        bars_df=panel,
        config={**_SELECTION_CONFIG, "use_lightgbm": False},
    )

    assert len(picks) == _SELECTION_CONFIG["top_k"]
    assert {item["model_engine"] for item in picks} == {"Prior-Ridge"}


def test_select_stocks_excludes_st_stocks() -> None:
    panel = _make_synthetic_panel().with_columns(
        pl.when(pl.col("asset") == "600001.SH")
        .then(True)
        .otherwise(pl.col("is_st"))
        .alias("is_st")
    )
    picks = MLMomentumReversalStrategy.select_stocks(
        bars_df=panel,
        config={**_SELECTION_CONFIG, "top_k": 20},
    )

    assets = {item["asset"] for item in picks}
    assert "600001.SH" not in assets
    assert len(picks) == 11


def test_select_stocks_excludes_up_limit_locked_stocks() -> None:
    panel = _lock_up_limit(_make_synthetic_panel(), "300001.SZ")
    picks = MLMomentumReversalStrategy.select_stocks(
        bars_df=panel,
        config={**_SELECTION_CONFIG, "top_k": 20},
    )

    assets = {item["asset"] for item in picks}
    assert "300001.SZ" not in assets
    assert len(picks) == 11


def test_filter_universe_market_cap_trim_then_st_and_suspension(monkeypatch) -> None:
    monkeypatch.setattr(
        ml_momentum_reversal.stock_list, "_data", pl.DataFrame()
    )
    panel = _make_synthetic_panel(n_assets=20)
    assets = sorted(panel["asset"].unique())
    caps = pl.DataFrame(
        {
            "asset": assets,
            "total_mv": [float(i + 1) * 1e9 for i in range(len(assets))],
        }
    )
    panel = panel.join(caps, on="asset", how="left")

    last_date = panel["date"].max()
    st_asset = assets[5]
    suspended_asset = assets[6]
    panel = panel.with_columns(
        pl.when((pl.col("asset") == st_asset) & (pl.col("date") == last_date))
        .then(True)
        .otherwise(pl.col("is_st"))
        .alias("is_st"),
        pl.when(
            (pl.col("asset") == suspended_asset) & (pl.col("date") == last_date)
        )
        .then(0.0)
        .otherwise(pl.col("volume"))
        .alias("volume"),
    )

    selected = ml_momentum_reversal._filter_universe(
        panel,
        last_date,
        universe_size=300,
        mode="market_cap_trim",
        trim_pct=0.05,
    )

    expected = set(assets[1:5] + assets[7:19])
    assert set(selected) == expected


def test_filter_universe_falls_back_to_liquidity_without_market_cap(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ml_momentum_reversal.stock_list, "_data", pl.DataFrame()
    )
    panel = _make_synthetic_panel(n_assets=20)
    selected = ml_momentum_reversal._filter_universe(
        panel,
        panel["date"].max(),
        universe_size=5,
        mode="market_cap_trim",
        trim_pct=0.05,
    )
    assert len(selected) == 5


# ---------------------------------------------------------------------------
# 5. on_bar execution: buys, T+1, barriers, time exit
# ---------------------------------------------------------------------------


class _FakeBroker:
    """Broker boundary fake that records orders and exposes portfolio state."""

    def __init__(
        self,
        *,
        cash: float = 1_000_000.0,
        positions: dict[str, object] | None = None,
    ) -> None:
        self.positions = dict(positions or {})
        self.cash = cash
        self.portfolio_id = "unit-test"
        self.buy_calls: list[tuple[str, float, dict[str, object]]] = []
        self.sell_calls: list[tuple[str, int, dict[str, object]]] = []

    async def buy_amount(self, asset: str, amount: float, **kwargs: object):
        self.buy_calls.append((asset, float(amount), kwargs))
        return SimpleNamespace(trades=[object()])

    async def sell(self, asset: str, shares: int, **kwargs: object):
        self.sell_calls.append((asset, int(shares), kwargs))
        return SimpleNamespace(trades=[object()])


def _make_strategy(
    broker: _FakeBroker, config: dict[str, object] | None = None
) -> MLMomentumReversalStrategy:
    strategy = MLMomentumReversalStrategy(broker, config)
    strategy.logs = []
    strategy.log = lambda msg, *args, **kwargs: strategy.logs.append(str(msg))
    return strategy


def _ranked_item(asset: str, score: float, rank: int = 1) -> dict[str, object]:
    return {
        "rank": rank,
        "asset": asset,
        "name": asset,
        "ml_score": score,
        "kline_pattern": "量价共振向上",
        "signal_reason": "板块超额+1.0% | MA20斜率向上(+1.0%) | 日内资金净流入",
    }


async def test_on_bar_buys_top_ranked_candidates_into_open_slots() -> None:
    broker = _FakeBroker(cash=100_000.0)
    strategy = _make_strategy(broker, {"top_k": 2, "use_lightgbm": False})
    ranked = [
        _ranked_item("600001.SH", 0.9, 1),
        _ranked_item("300001.SZ", 0.8, 2),
        _ranked_item("688001.SH", 0.7, 3),
    ]
    strategy._rank_for_bar = lambda tm: (
        ranked,
        {"600001.SH": 10.0, "300001.SZ": 20.0, "688001.SH": 30.0},
        {"600001.SH": 1, "300001.SZ": 2, "688001.SH": 3},
    )

    await strategy.on_bar(dt.datetime(2024, 6, 3, 10, 0))

    assert [asset for asset, _amount, _kwargs in broker.buy_calls] == [
        "600001.SH",
        "300001.SZ",
    ]
    for _asset, amount, _kwargs in broker.buy_calls:
        assert amount == pytest.approx(49_000.0)


async def test_on_bar_defers_price_barrier_to_broker_overlay() -> None:
    """With broker barriers enabled, the strategy leaves price exits to the overlay."""
    position = SimpleNamespace(shares=100, avail=100, price=10.0, mv=900.0)
    broker = _FakeBroker(cash=0.0, positions={"600001.SH": position})
    strategy = _make_strategy(
        broker, {"use_lightgbm": False, "use_broker_barriers": True}
    )
    strategy._rank_for_bar = lambda tm: (
        [],
        {"600001.SH": 9.0},
        {"600001.SH": 1},
    )

    await strategy.on_bar(dt.datetime(2024, 6, 3, 10, 0))

    assert broker.sell_calls == []
    assert not any("止损" in message for message in strategy.logs)


async def test_on_bar_keeps_rank_exit_when_broker_barriers_enabled() -> None:
    position = SimpleNamespace(shares=100, avail=100, price=10.0, mv=1000.0)
    broker = _FakeBroker(cash=0.0, positions={"600001.SH": position})
    strategy = _make_strategy(
        broker, {"use_lightgbm": False, "use_broker_barriers": True}
    )
    strategy._rank_for_bar = lambda tm: (
        [],
        {"600001.SH": 10.0, "600002.SH": 10.0},
        {"600001.SH": 99, "600002.SH": 1},
    )

    await strategy.on_bar(dt.datetime(2024, 6, 3, 10, 0))

    assert [(asset, shares) for asset, shares, _kw in broker.sell_calls] == [
        ("600001.SH", 100)
    ]
    assert any("排名跌出" in message for message in strategy.logs)


# ---------------------------------------------------------------------------
# 6. Web stock-selection modal & routes
# ---------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, form_data: dict[str, str]) -> None:
        self._form_data = form_data

    async def form(self) -> dict[str, str]:
        return self._form_data


def _sample_picks() -> list[dict[str, object]]:
    return [
        {
            "rank": 1,
            "asset": "600001.SH",
            "name": "示例股份",
            "board": "主板",
            "close": 10.0,
            "ret_1d": 0.01,
            "ret_5d": 0.02,
            "ret_20d": 0.05,
            "ml_score": 0.8123,
            "rs_board_20d": 0.03,
            "kline_pattern": "低开阳线弱转强",
            "signal_reason": "板块超额+5.0% | MA20斜率向上(+1.0%) | 日内资金净流入",
            "as_of_date": "2024-12-31",
            "model_engine": "LightGBM",
            "tp_price": 11.5,
            "sl_price": 9.4,
            "barrier_horizon": 3,
            "tb_barrier_text": "止盈:11.50(+15%) / 止损:9.40(-6%) / 3日",
        }
    ]


def _install_selection_mocks(monkeypatch, picks, calls=None):
    monkeypatch.setattr(
        strategy_page.strategy_loader,
        "load_from_cache",
        lambda: {"MLMomentumReversalStrategy": MLMomentumReversalStrategy},
    )

    def _fake_select(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return list(picks)

    monkeypatch.setattr(
        MLMomentumReversalStrategy,
        "select_stocks",
        staticmethod(_fake_select),
    )


def test_build_strategy_rows_renders_stock_selection_button(monkeypatch) -> None:
    _install_selection_mocks(monkeypatch, _sample_picks())
    monkeypatch.setattr(
        strategy_page.db,
        "get_portfolios_by_strategy",
        lambda name: pl.DataFrame(),
    )

    rows = strategy_page._build_strategy_rows(
        {"MLMomentumReversalStrategy": MLMomentumReversalStrategy}
    )
    html = to_xml(rows[0])

    assert "选股" in html
    assert 'hx-get="/strategy/MLMomentumReversalStrategy/select/modal"' in html


def test_strategy_select_modal_renders_triple_barrier_table(monkeypatch) -> None:
    _install_selection_mocks(monkeypatch, _sample_picks())

    html = to_xml(
        strategy_page.strategy_select_modal("MLMomentumReversalStrategy")
    )

    assert 'id="stock-selection-table"' in html
    assert "600001.SH" in html
    assert "示例股份" in html
    assert "低开阳线弱转强" in html
    assert "止盈:11.50(+15%) / 止损:9.40(-6%) / 3日" in html
    assert "0.812" in html
    assert "模型引擎: LightGBM" in html


async def test_run_strategy_select_uses_submitted_params(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    _install_selection_mocks(monkeypatch, _sample_picks(), calls=calls)
    request = FakeRequest(
        {
            "as_of_date": "2024-12-31",
            "param_top_k": "3",
            "param_universe_size": "50",
            "param_train_window": "30",
            "param_horizon": "3",
            "param_take_profit_pct": "0.15",
            "param_stop_loss_pct": "0.06",
            "param_use_lightgbm": "true",
        }
    )

    html = to_xml(
        await strategy_page.run_strategy_select(
            request, "MLMomentumReversalStrategy"
        )
    )

    assert calls[0]["config"]["top_k"] == 3
    assert calls[0]["config"]["use_lightgbm"] is True
    assert calls[0]["as_of_date"] == "2024-12-31"
    assert 'id="stock-selection-table"' in html
    assert "600001.SH" in html
    assert "止盈:11.50(+15%) / 止损:9.40(-6%) / 3日" in html


def test_strategy_select_modal_warns_when_requested_date_exceeds_local_data(
    monkeypatch,
) -> None:
    _install_selection_mocks(monkeypatch, _sample_picks())
    monkeypatch.setattr(
        strategy_page,
        "_get_default_selection_date",
        lambda: "2024-12-31",
    )

    html = to_xml(
        strategy_page._render_stock_selection_modal(
            "MLMomentumReversalStrategy",
            MLMomentumReversalStrategy,
            as_of_date="2026-10-20",
            auto_run=True,
        )
    )

    assert "请求日期 2026-10-20 晚于本地最新行情 2024-12-31" in html
    assert "已自动使用最近可用交易日 2024-12-31 选股" in html
    assert "选股基准日: 2024-12-31" in html


def test_strategy_select_modal_exposes_loading_indicator(monkeypatch) -> None:
    _install_selection_mocks(monkeypatch, _sample_picks())
    monkeypatch.setattr(
        strategy_page,
        "_get_default_selection_date",
        lambda: "2024-12-31",
    )

    html = to_xml(
        strategy_page.strategy_select_modal("MLMomentumReversalStrategy")
    )

    assert 'id="select-loading"' in html
    assert 'hx-indicator="#select-loading"' in html
    assert 'hx-disabled-elt="this"' in html
    assert "正在拉取行情、计算 24 个因子并运行模型" in html


def test_selection_date_notice_same_day_is_empty() -> None:
    assert (
        strategy_page._resolve_selection_date_notice(
            "2024-12-31",
            "2024-12-31",
            "2024-12-31",
            has_picks=True,
        )
        == ""
    )


def test_build_strategy_rows_marks_global_loading_indicator(monkeypatch) -> None:
    _install_selection_mocks(monkeypatch, _sample_picks())
    monkeypatch.setattr(
        strategy_page.db,
        "get_portfolios_by_strategy",
        lambda name: pl.DataFrame(),
    )

    rows = strategy_page._build_strategy_rows(
        {"MLMomentumReversalStrategy": MLMomentumReversalStrategy}
    )
    html = to_xml(rows[0])

    assert 'hx-indicator="#global-loading"' in html
