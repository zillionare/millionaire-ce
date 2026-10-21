"""系统维护 - 因子台账页面测试。"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantide.factors import FEATURE_COLS
from quantide.factors.alphatest import (
    DecileBucket,
    RankIcSummary,
    SingleFactorResult,
)
from quantide.web.pages.system import alpha_ledger


class _FakeRequest:
    def __init__(self, query_params: dict[str, list[str]] | None = None):
        self.query_params = _QueryParams(query_params or {})


class _QueryParams:
    def __init__(self, data: dict[str, list[str]]):
        self._data = data

    def get(self, key: str, default: str = "") -> str:
        values = self._data.get(key)
        return values[0] if values else default

    def getlist(self, key: str) -> list[str]:
        return list(self._data.get(key, []))


def test_parse_holdings_defaults_and_filters() -> None:
    assert alpha_ledger._parse_holdings("") == list(alpha_ledger.DEFAULT_HOLDINGS)
    assert alpha_ledger._parse_holdings("3,1,3,x,0") == [1, 3]


def test_build_form_contains_factor_and_holding_controls() -> None:
    html = alpha_ledger._build_form(
        "2022-06-01", "2026-10-19", [1, 3], list(FEATURE_COLS)
    ).__html__()

    assert 'name="factors"' in html
    assert 'name="holdings"' in html
    assert 'name="run"' in html
    assert 'value="ma20_slope"' in html


def test_build_result_table_renders_ic_t_spread_and_years() -> None:
    results = [
        SingleFactorResult(
            factor="ma20_slope",
            holding_days=3,
            price="open",
            rank_ic=RankIcSummary(mean=-0.0472, t_stat=-7.4, n_days=714),
            deciles=[
                DecileBucket(bucket=1, n_samples=100, mean_forward=0.0029),
                DecileBucket(bucket=10, n_samples=100, mean_forward=-0.0007),
            ],
            by_year={
                2023: RankIcSummary(mean=0.0089, t_stat=0.6, n_days=44),
                2024: RankIcSummary(mean=-0.0501, t_stat=-3.9, n_days=242),
            },
            n_rows=100,
        )
    ]

    html = alpha_ledger._build_result_table(results, [3]).__html__()

    assert "ma20_slope" in html
    assert "-0.0472" in html
    assert "t=-7.4" in html
    assert "+0.36%" in html  # D1-D10 价差
    assert "+0.0089" in html  # 2023
    assert "-0.0501" in html  # 2024


def test_build_result_table_handles_empty() -> None:
    html = alpha_ledger._build_result_table([], [1]).__html__()

    assert "没有可展示的结果" in html


def test_run_ledger_uses_cache(monkeypatch) -> None:
    calls: list[tuple] = []

    def _fake_panel(start, end):
        calls.append((start, end))
        return pl.DataFrame({"date": [dt.date(2024, 1, 1)]})

    def _fake_study(panel, factors, holding_days, price="open", buckets=10):
        return []

    monkeypatch.setattr(alpha_ledger, "load_factor_panel", _fake_panel)
    monkeypatch.setattr(alpha_ledger, "study_factors", _fake_study)
    alpha_ledger._LEDGER_CACHE.clear()

    alpha_ledger._run_ledger("2024-01-01", "2024-06-30", [1], ["bias_5"])
    alpha_ledger._run_ledger("2024-01-01", "2024-06-30", [1], ["bias_5"])

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_index_renders_page_without_results(monkeypatch) -> None:
    result = await alpha_ledger.index(_FakeRequest())
    parts = result if isinstance(result, tuple) else (result,)
    html = "".join(
        part.__html__() if hasattr(part, "__html__") else str(part) for part in parts
    )

    assert "因子台账" in html
    assert "开始检验" in html
    assert "没有可展示的结果" in html
    assert len(FEATURE_COLS) == 28
