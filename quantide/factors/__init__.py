"""因子库：原始 K 线 → 因子表，与策略解耦。"""

from quantide.factors.alpha import (
    FACTOR_VERSION,
    FEATURE_COLS,
    FEATURE_DOCS,
    FEATURE_GROUPS,
    compute_alpha_features,
    standardize_features,
)
from quantide.factors.catalog import (
    FeatureSpec,
    list_feature_specs,
    resolve_feature_selection,
)
from quantide.factors.single_factor import (
    SingleFactorResult,
    load_factor_panel,
    study_single_factor,
)

__all__ = [
    "FACTOR_VERSION",
    "FEATURE_COLS",
    "FEATURE_DOCS",
    "FEATURE_GROUPS",
    "FeatureSpec",
    "SingleFactorResult",
    "load_factor_panel",
    "study_single_factor",
    "compute_alpha_features",
    "list_feature_specs",
    "resolve_feature_selection",
    "standardize_features",
]
