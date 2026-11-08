"""Durable run worker wiring for factor tracking."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from quantide.factors.metadata import FACTOR_METADATA
from quantide.service import factor_tracking as ft
from quantide.service.factor_runs import (
    CANCELLED,
    FactorRun,
    FactorRunStore,
    FactorRunWorker,
    RunBudget,
    RunFence,
)
from quantide.service.worker_slot import WorkerSlot

BUDGET = RunBudget()
_LOCK = threading.RLock()
_STORE: FactorRunStore | None = None


def run_store() -> FactorRunStore:
    global _STORE
    root = ft._root()
    with _LOCK:
        if _STORE is None or _STORE.path != root / "runs.sqlite":
            _STORE = FactorRunStore(root / "runs.sqlite")
        return _STORE


def _execute_run(
    run: FactorRun,
    cancel: threading.Event,
    checkpoint: Any,
) -> str:
    factor_ids = list(run.factor_ids)

    fence = RunFence(run_store(), run.run_id, run.owner)

    with ft._REFRESH_LOCK:
        ft._REFRESHING.update(factor_ids)
    try:
        if callable(checkpoint):
            checkpoint("start", {"factors": factor_ids, "lookback": run.lookback})
        if run.scope == "factor" and len(factor_ids) == 1:
            state = ft.refresh_factor(
                factor_ids[0],
                should_cancel=cancel.is_set,
                checkpoint=checkpoint,
                fence=fence,
            )
            status = ft._attempt_status(state.get("last_attempt")) or "failed"
        else:
            ft._refresh_group(
                factor_ids,
                run.lookback,
                should_cancel=cancel.is_set,
                checkpoint=checkpoint,
                fence=fence,
            )
            statuses = [
                ft._attempt_status(ft.load_state(factor_id).get("last_attempt"))
                for factor_id in factor_ids
            ]
            if "cancelled" in statuses or "superseded" in statuses:
                status = "cancelled"
            elif "failed" in statuses:
                status = "failed"
            elif "insufficient" in statuses:
                status = "insufficient"
            else:
                status = "succeeded"
    finally:
        with ft._REFRESH_LOCK:
            ft._REFRESHING.difference_update(factor_ids)
        ft._release_pending(factor_ids)
    if cancel.is_set():
        return CANCELLED
    return status if status in {"succeeded", "failed", "insufficient"} else "failed"


_SLOT = WorkerSlot(
    lambda: ft._root() / "runs.sqlite",
    lambda: FactorRunWorker(run_store(), _execute_run, budget=BUDGET, scopes=("factor", "group")),
)


def ensure_worker() -> FactorRunWorker:
    return _SLOT.ensure()


def stop_worker() -> None:
    _SLOT.stop()


def enqueue(
    factor_ids: list[str],
    *,
    lookback: str | None = None,
    profile_revisions: dict[str, int] | None = None,
) -> list[str]:
    profiles = {factor_id: ft.load_profile(factor_id) for factor_id in factor_ids}
    if lookback is None:
        lookbacks = {profile.lookback for profile in profiles.values()}
        if len(lookbacks) != 1:
            raise ValueError("factor run requires one lookback")
        lookback = lookbacks.pop()
    runs = run_store().enqueue(
        factor_ids,
        lookback=lookback,
        profile_revisions={
            factor_id: (profile_revisions or {}).get(factor_id, profile.revision)
            for factor_id, profile in profiles.items()
        },
        source_revision=ft._manifest_revision(),
        source_versions=ft._source_versions(),
        budget=BUDGET,
    )
    if runs:
        ensure_worker()
    return runs


def trigger_refresh(factor_id: str) -> bool:
    profile = ft.load_profile(factor_id)
    return bool(enqueue([factor_id], lookback=profile.lookback))


def cancel_refresh(factor_id: str) -> bool:
    return bool(run_store().cancel_factors([factor_id]))


def trigger_refresh_all() -> int:
    groups: dict[str, list[str]] = {}
    for factor_id in FACTOR_METADATA:
        profile = ft.load_profile(factor_id)
        if profile.enabled:
            groups.setdefault(profile.lookback, []).append(factor_id)
    count = 0
    for lookback, factor_ids in groups.items():
        if enqueue(factor_ids, lookback=lookback):
            count += len(factor_ids)
    return count


def recover() -> int:
    return run_store().recover()


def list_runs(status: str | None = None, limit: int = 100) -> list[FactorRun]:
    return run_store().list_runs(status=status, limit=limit)


def runs_path() -> Path:
    return run_store().path
