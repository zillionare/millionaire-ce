"""ModelStore 持久化测试。"""

from __future__ import annotations

import datetime

from quantide.service.model_store import (
    ModelArtifact,
    ModelStore,
    model_key,
)


def test_model_key_changes_with_model_config():
    key1 = model_key("Demo", {"train_window": 45, "retrain_days": 5, "commission": 1e-4})
    key2 = model_key("Demo", {"train_window": 30, "retrain_days": 5, "commission": 1e-4})
    key3 = model_key("Demo", {"train_window": 45, "retrain_days": 5, "commission": 2e-4})
    assert key1 != key2
    assert key1 == key3  # 交易费用不影响模型版本


def test_model_store_saves_and_overwrites_latest(tmp_path):
    store = ModelStore(tmp_path)
    key = "Demo-abc123"
    meta1 = ModelArtifact(
        model_key=key,
        strategy_name="Demo",
        config_hash="abc123",
        train_end="2026-10-24",
        effective_from="2026-10-27",
        engine="LightGBM",
    )
    store.save(key=key, model={"version": 1}, metadata=meta1)
    model, loaded = store.load(key)
    assert model == {"version": 1}
    assert loaded is not None
    assert loaded.train_end == "2026-10-24"

    meta2 = ModelArtifact(
        model_key=key,
        strategy_name="Demo",
        config_hash="abc123",
        train_end="2026-10-27",
        effective_from="2026-10-28",
        engine="LightGBM",
    )
    store.save(key=key, model={"version": 2}, metadata=meta2)
    model, loaded = store.load(key)
    assert model == {"version": 2}
    assert loaded is not None
    assert loaded.effective_from == "2026-10-28"


def test_model_store_missing_and_clear(tmp_path):
    store = ModelStore(tmp_path)
    assert store.load("missing") == (None, None)
    meta = ModelArtifact(
        model_key="k",
        strategy_name="Demo",
        config_hash="c",
        train_end="2026-10-24",
        effective_from="2026-10-27",
    )
    store.save(key="k", model=1, metadata=meta)
    store.clear("k")
    assert store.load("k") == (None, None)


def test_model_store_handles_corrupt_metadata(tmp_path):
    store = ModelStore(tmp_path)
    directory = store.root / "bad"
    directory.mkdir(parents=True)
    (directory / "latest.json").write_text("{not-json", encoding="utf-8")
    (directory / "latest.pkl").write_bytes(b"x")
    model, meta = store.load("bad")
    assert model is None
    assert meta is None
