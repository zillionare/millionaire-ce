"""ML 策略模型落盘/加载测试。"""

from __future__ import annotations

import datetime

import polars as pl

from quantide.service import model_store as model_store_module
from quantide.strategies.example.ml_momentum_reversal import (
    MLMomentumReversalStrategy,
)


class _Broker:
    portfolio_id = "test-model"
    positions = {}


def test_strategy_saves_and_loads_latest_model(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store_module.model_store, "root", tmp_path)
    strategy = MLMomentumReversalStrategy(_Broker(), {"retrain_days": 5})
    training = pl.DataFrame(
        {
            "date": [datetime.date(2026, 10, 24)],
            "asset": ["000001.SZ"],
        }
    )
    strategy._save_model_artifact(
        {"model": "v1"},
        "LightGBM",
        training,
        effective_from=datetime.date(2026, 10, 27),
    )
    assert strategy._model_train_end == datetime.date(2026, 10, 24)
    assert strategy._model_effective_from == datetime.date(2026, 10, 27)

    reloaded = MLMomentumReversalStrategy(_Broker(), {"retrain_days": 5})
    assert reloaded._load_model_from_store(datetime.date(2026, 10, 27)) is True
    assert reloaded._model == {"model": "v1"}
    assert reloaded._model_train_end == datetime.date(2026, 10, 24)


def test_strategy_rejects_future_effective_model(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store_module.model_store, "root", tmp_path)
    strategy = MLMomentumReversalStrategy(_Broker(), {"retrain_days": 5})
    training = pl.DataFrame(
        {
            "date": [datetime.date(2026, 10, 27)],
            "asset": ["000001.SZ"],
        }
    )
    strategy._save_model_artifact(
        {"model": "v2"},
        "LightGBM",
        training,
        effective_from=datetime.date(2026, 10, 28),
    )

    reloaded = MLMomentumReversalStrategy(_Broker(), {"retrain_days": 5})
    assert reloaded._load_model_from_store(datetime.date(2026, 10, 27)) is False
    assert reloaded._model is None
    assert reloaded._model_pending is True


def test_strategy_rejects_model_with_invalid_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store_module.model_store, "root", tmp_path)
    strategy = MLMomentumReversalStrategy(_Broker(), {"retrain_days": 5})
    training = pl.DataFrame(
        {"date": [datetime.date(2026, 10, 24)], "asset": ["000001.SZ"]}
    )
    strategy._save_model_artifact(
        {"model": "bad-metadata"},
        "LightGBM",
        training,
        effective_from=datetime.date(2026, 10, 27),
    )
    metadata = model_store_module.model_store.metadata(strategy._model_key)
    assert metadata is not None
    metadata.effective_from = "not-a-date"
    model_store_module.model_store.save(
        key=strategy._model_key, model={"model": "bad-metadata"}, metadata=metadata
    )

    reloaded = MLMomentumReversalStrategy(_Broker(), {"retrain_days": 5})
    assert reloaded._load_model_from_store(datetime.date(2026, 10, 27)) is False
    assert reloaded._model is None
