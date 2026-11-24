"""Per-feature-family Isolation Forests (model iforest-v2, Phase 6B).

Why not one forest over all nine features: an Isolation Forest picks a random feature at
every split, so an anomaly that is extreme in ONE family (a sub-second burst) but ordinary
in the others gets isolated only by the minority of splits that use its family, and its
score stays in the normal range. Adding velocity features to a single forest (tried as an
intermediate step) lifted burst recall from 0.00 to 0.82 but dropped large-amount recall
from 0.78 to 0.45, the same dilution in reverse.

So each family gets its own forest, and each forest's raw unusualness is converted to its
PERCENTILE among the training transfers. The combined score is the maximum over families:
"more unusual than X % of training transfers in at least one respect". Percentiles make the
families comparable without assuming anything about their score scales. Still unsupervised:
labels are never used to fit; the threshold is chosen on validation data.
"""

from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import IsolationForest

FAMILIES: dict[str, tuple[str, ...]] = {
    "amount": ("log_amount", "amount_vs_baseline", "log_prior_transfers", "log_account_age"),
    "velocity": (
        "log_secs_since_prev",
        "gap_vs_typical",
        "burst_10s",
        "rate_vs_typical_60s",
        "log_prior_transfers",
    ),
}


@dataclass
class GroupedIsolationForest:
    """Picklable ensemble; `anomaly_score(x)` returns a score in [0, 1] per row of x,
    whose columns are `feature_names` (the model metadata's feature order)."""

    feature_names: tuple[str, ...]
    seed: int
    n_estimators: int = 200
    max_samples: int = 256
    families: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(FAMILIES))
    forests_: dict[str, IsolationForest] = field(default_factory=dict)
    reference_: dict[str, np.ndarray] = field(default_factory=dict)

    def _columns(self, family: str) -> list[int]:
        return [self.feature_names.index(n) for n in self.families[family]]

    def fit(self, x: np.ndarray) -> "GroupedIsolationForest":
        for family in self.families:
            cols = self._columns(family)
            forest = IsolationForest(
                n_estimators=self.n_estimators,
                max_samples=self.max_samples,
                contamination="auto",
                random_state=self.seed,
            ).fit(x[:, cols])
            self.forests_[family] = forest
            self.reference_[family] = np.sort(-forest.score_samples(x[:, cols]))
        return self

    def family_scores(self, x: np.ndarray) -> dict[str, np.ndarray]:
        out = {}
        for family, forest in self.forests_.items():
            raw = -forest.score_samples(x[:, self._columns(family)])
            ref = self.reference_[family]
            out[family] = np.searchsorted(ref, raw, side="right") / len(ref)
        return out

    def anomaly_score(self, x: np.ndarray) -> np.ndarray:
        return np.max(np.vstack(list(self.family_scores(x).values())), axis=0)
