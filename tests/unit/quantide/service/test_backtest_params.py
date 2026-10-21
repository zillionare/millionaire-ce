"""回测参数与默认值比较（quantide.service.backtest_params）测试。"""

from __future__ import annotations

import pytest

from quantide.factors import FEATURE_COLS
from quantide.service.backtest_params import (
    changed_params,
    default_backtest_params,
    format_changed_params,
)


class _DemoStrategy:
    PARAMS = {
        "top_k": 5,
        "keep_top_n": 15,
        "universe_size": 2000,
        "stop_loss_pct": 0.06,
    }


def test_default_backtest_params_merges_strategy_fee_and_factors() -> None:
    defaults = default_backtest_params(_DemoStrategy)

    assert defaults["top_k"] == 5
    assert defaults["commission"] == pytest.approx(1e-4)
    assert defaults["features"] == list(FEATURE_COLS)


def test_changed_params_ignores_defaults_and_fee_params() -> None:
    defaults = default_backtest_params(_DemoStrategy)
    config = {
        "top_k": 5,
        "keep_top_n": 20,
        "universe_size": 2000,
        "stop_loss_pct": 0.06,
        "commission": 1e-4,
        "stamp_duty": 5e-4,
        "features": list(FEATURE_COLS),
    }

    changed = changed_params(config, defaults)

    assert changed == {"keep_top_n": 20}


def test_changed_params_reports_factor_subset() -> None:
    defaults = default_backtest_params(_DemoStrategy)
    config = {"features": ["bias_5", "ma20_slope"], "top_k": 5}

    changed = changed_params(config, defaults)

    assert list(changed) == ["features"]


def test_format_changed_params_text() -> None:
    defaults = default_backtest_params(_DemoStrategy)

    assert format_changed_params({"top_k": 5}, defaults) == "默认"
    assert (
        format_changed_params({"top_k": 3, "keep_top_n": 15}, defaults) == "top_k=3"
    )
    assert (
        format_changed_params(
            {"features": ["bias_5", "ma20_slope"]}, defaults
        )
        == "因子=2项"
    )


def test_format_changed_params_tolerates_float_noise() -> None:
    defaults = default_backtest_params(_DemoStrategy)

    text = format_changed_params({"stop_loss_pct": 0.060000000001}, defaults)

    assert text == "默认"
