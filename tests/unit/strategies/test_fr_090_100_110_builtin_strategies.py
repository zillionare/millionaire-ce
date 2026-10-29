"""FR-090 双均线 (CE built-in strategy).

按 spec-strategy.md §FR-090:
- 双均线: fast 上穿 slow → 买入, slow 上穿 fast → 卖出.
  参数 [fast, slow] 默认 [5, 20].
"""

from __future__ import annotations

from quantide.core.strategy import BaseStrategy
from quantide.strategies.example.dual_ma import DualMAStrategy


def test_fr_090_dual_ma_is_base_strategy():
    """AC-FR-090: DualMAStrategy 是 BaseStrategy 子类."""
    assert issubclass(DualMAStrategy, BaseStrategy)


def test_fr_090_dual_ma_default_config():
    """AC-FR-090: default_config 默认参数 [fast=5, slow=20]."""
    cfg = DualMAStrategy.default_config()
    assert "fast" in cfg
    assert "slow" in cfg
    assert cfg["fast"] == 5
    assert cfg["slow"] == 20


def test_fr_090_dual_ma_signature():
    """AC-FR-090: DualMAStrategy 提供 on_bar / on_day_open 回调."""
    assert hasattr(DualMAStrategy, "on_bar")
    assert hasattr(DualMAStrategy, "on_day_open")
