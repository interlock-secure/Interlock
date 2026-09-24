"""The recoverability model, and why calibration matters more than accuracy.

What the model is for
---------------------
An analyst has several hundred open cases and capacity for a handful in the
next hour. The model answers one question: which ones?

That makes this a ranking problem under a capacity constraint, not a
classification problem, and the distinction changes what "good" means.
Accuracy is close to irrelevant - a model that predicts "not recovered" for
everything is 92% accurate on this data and useless. What matters is whether
the probability it assigns is *right*, because the queue is ordered by
expected value, which is probability multiplied by amount. A model that is
confidently wrong about a large case sends an analyst to the wrong place.

So the headline metric is Brier score, not AUC, and the model is calibrated
explicitly.

Why two models
--------------
A gradient-boosted tree and a logistic regression, reported side by side.
The logistic regression is the honest baseline: if it matches the tree, the
extra complexity is unjustified and should be said out loud rather than
buried. Reporting only the winner is how projects end up claiming a model
earns its keep when a linear fit would have done.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from interlock.generator.recall_cases import GeneratedCase
from interlock.triage.features import feature_matrix

RANDOM_STATE = 20260919

CALIBRATION_FRACTION = 0.25
"""Share of the training split held back to calibrate on.

Calibrating on the same rows the model fitted would produce a calibrator
learning the model's training-set optimism rather than its real error, and the
resulting probabilities would look excellent and be wrong. The held-back slice
is taken from the *end* of the training period, keeping the split temporal all
the way down.
"""


@dataclass(frozen=True, slots=True)
class ModelReport:
    """How a fitted model performed. Every number the evaluation will quote."""

    name: str
    brier: float
    """Mean squared error of the predicted probabilities. The headline: lower
    is better, and it punishes confident wrongness far harder than AUC."""

    auc: float
    """Reported because everyone asks. It measures ordering only, and says
    nothing about whether a 0.8 means 0.8."""

    positive_rate: float
    mean_predicted: float
    """Against positive_rate, this is the crudest calibration check there is:
    if a model predicts a mean probability of 0.30 on a population where 8%
    recover, it is badly miscalibrated however good its AUC."""

    n_train: int
    n_test: int

    def summary(self) -> str:
        return (
            f"{self.name}: Brier {self.brier:.4f}, AUC {self.auc:.3f}, "
            f"predicts {self.mean_predicted:.1%} against an actual {self.positive_rate:.1%}"
        )


class RecoverabilityModel:
    """Predicts whether funds are still recoverable on a case.

    Wraps a scikit-learn estimator rather than exposing it, so callers cannot
    reach past the calibration or feed it a raw matrix with the columns in a
    different order.
    """

    def __init__(self, *, kind: str = "boosted") -> None:
        if kind not in {"boosted", "linear"}:
            raise ValueError(f"unknown model kind {kind!r}; expected 'boosted' or 'linear'")
        self.kind = kind
        self._fitted: CalibratedClassifierCV | None = None
        self._fallback_rate: float | None = None
        self._closed: list[GeneratedCase] = []

    def _base_estimator(self):
        if self.kind == "boosted":
            return HistGradientBoostingClassifier(
                # Small and shallow on purpose. A few thousand rows with
                # fifteen features does not support a large ensemble, and an
                # over-fitted model would make the calibration step's job
                # impossible.
                max_iter=180,
                max_depth=4,
                learning_rate=0.06,
                min_samples_leaf=25,
                l2_regularization=1.0,
                random_state=RANDOM_STATE,
            )
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=2000, random_state=RANDOM_STATE),
        )

    def fit(self, cases: list[GeneratedCase]) -> RecoverabilityModel:
        """Fit on the training split, calibrating on a held-back temporal tail.

        Cases must already be the training split; this does not filter, so a
        caller passing everything trains on the test set and the evaluation
        will silently flatter itself.
        """
        ordered = sorted(cases, key=lambda c: c.received_at)
        cut = int(len(ordered) * (1 - CALIBRATION_FRACTION))
        fit_rows, calibration_rows = ordered[:cut], ordered[cut:]

        # Counterparty estimates for every row come only from training cases
        # that had closed before that row arrived. The first fix built one
        # table from all fitting rows, so each row's estimate included its own
        # label; feature_matrix now does the as-of sweep itself.
        self._closed = ordered

        x_fit, y_fit, _ = feature_matrix(fit_rows, self._closed)
        x_cal, y_cal, _ = feature_matrix(calibration_rows, self._closed)

        # A split with one class in it cannot train anything. Fall back to the
        # base rate and say so through predict(), rather than raising in the
        # middle of an evaluation run.
        if len(np.unique(y_fit)) < 2 or len(np.unique(y_cal)) < 2:
            self._fallback_rate = float(np.mean([c.recovered for c in ordered])) if ordered else 0.0
            return self

        base = self._base_estimator().fit(x_fit, y_fit)

        # FrozenEstimator so the calibrator fits on the held-back rows alone
        # and does not refit the model underneath it - which would put the
        # calibration data back into training and defeat the whole point.
        self._fitted = CalibratedClassifierCV(
            estimator=FrozenEstimator(base), method="isotonic"
        ).fit(x_cal, y_cal)
        return self

    def predict_proba(self, cases: list[GeneratedCase]) -> np.ndarray:
        """Probability that each case is recoverable."""
        if not cases:
            return np.zeros(0)
        if self._fitted is None:
            rate = self._fallback_rate if self._fallback_rate is not None else 0.0
            return np.full(len(cases), rate)
        x, _, _ = feature_matrix(cases, self._closed)
        return self._fitted.predict_proba(x)[:, 1]

    def evaluate(self, test_cases: list[GeneratedCase], *, n_train: int) -> ModelReport:
        """Score the model on held-out cases."""
        probabilities = self.predict_proba(test_cases)
        actual = np.array([c.recovered for c in test_cases], dtype=bool)

        # AUC is undefined when one class is absent; report 0.5 rather than
        # letting an exception abort a whole evaluation sweep.
        auc = float(roc_auc_score(actual, probabilities)) if len(np.unique(actual)) > 1 else 0.5

        return ModelReport(
            name={"boosted": "gradient boosting", "linear": "logistic regression"}[self.kind],
            brier=float(brier_score_loss(actual, probabilities)),
            auc=auc,
            positive_rate=float(np.mean(actual)) if len(actual) else 0.0,
            mean_predicted=float(np.mean(probabilities)) if len(probabilities) else 0.0,
            n_train=n_train,
            n_test=len(test_cases),
        )


def calibration_table(
    probabilities: np.ndarray, actual: np.ndarray, *, bins: int = 10
) -> list[dict[str, float]]:
    """Predicted against observed, by probability decile.

    The table an interviewer should ask for. A model whose 0.7 bucket recovers
    30% of the time is not usable for ranking by expected value, however good
    its AUC, and only this view shows it.
    """
    if len(probabilities) == 0:
        return []

    edges = np.linspace(0.0, 1.0, bins + 1)
    rows: list[dict[str, float]] = []

    for low, high in pairwise(edges):
        in_bin = (probabilities >= low) & (
            (probabilities < high) if high < 1.0 else (probabilities <= high)
        )
        count = int(np.sum(in_bin))
        if count == 0:
            continue
        rows.append(
            {
                "bin_low": float(low),
                "bin_high": float(high),
                "count": float(count),
                "mean_predicted": float(np.mean(probabilities[in_bin])),
                "observed_rate": float(np.mean(actual[in_bin])),
            }
        )

    return rows
