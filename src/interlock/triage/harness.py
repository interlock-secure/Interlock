"""The evaluation run, across seeds.

One seed proves nothing. A model that beats arrival order on a single draw of
the data may be reading noise, and the honest bar - stated in BUILD_PLAN M6 -
is that it beats arrival order by more than the seed-to-seed variance.

So the harness builds the case population from several seeds, fits and scores
independently on each, and reports the spread alongside the mean. If the
margin does not clear the spread, that is the finding and it gets reported as
one rather than smoothed away.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from interlock.generator.config import INFLATED_ARM, ArmSpec
from interlock.generator.recall_cases import generate_cases
from interlock.generator.testbed import WINDOW_START, build_arm
from interlock.triage.features import temporal_split
from interlock.triage.model import ModelReport, RecoverabilityModel, calibration_table
from interlock.triage.policy import (
    DEFAULT_CAPACITY_SWEEP,
    PolicyResult,
    sweep,
)

DEFAULT_SEEDS: tuple[int, ...] = (11, 23, 37, 59, 71)
"""Five seeds. Enough for a usable spread without the run taking minutes."""


@dataclass(frozen=True, slots=True)
class SeedRun:
    """One seed's complete result."""

    seed: int
    model_report: ModelReport
    baseline_report: ModelReport
    results: list[PolicyResult]
    calibration: list[dict[str, float]] = field(default_factory=list)

    def recovered_at(self, policy: str, capacity: int) -> int:
        for r in self.results:
            if r.policy == policy and r.capacity == capacity:
                return r.recovered_cents
        raise KeyError(f"no result for {policy} at capacity {capacity}")


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Every seed, plus the aggregate the report quotes."""

    runs: list[SeedRun]
    capacities: tuple[int, ...]

    def margin_over(self, baseline: str, capacity: int) -> tuple[float, float, float]:
        """Mean dollar advantage over a baseline, its spread, and the worst seed.

        Dollars, not a ratio. Arrival order recovers so close to nothing at low
        capacity that a ratio against it reaches three figures, which is
        arithmetically true and rhetorically worthless - the denominator is
        noise. An absolute advantage is comparable across capacities and cannot
        be inflated by a small baseline.

        The worst seed is included deliberately. A mean that clears the bar
        while one seed loses outright is a different claim from one that wins
        everywhere, and only the minimum distinguishes them.
        """
        deltas = [
            (run.recovered_at("model", capacity) - run.recovered_at(baseline, capacity)) / 100.0
            for run in self.runs
        ]
        return float(np.mean(deltas)), float(np.std(deltas)), float(np.min(deltas))

    def never_loses_to(self, baseline: str, capacity: int) -> bool:
        """Ahead on average and behind on no seed.

        Applied against ``amount`` as well as ``arrival``, because largest-first
        is the real competitor: it needs no model, no training data and no
        maintenance, so a model that merely matches it has not earned its place.

        An earlier version asked only whether the mean exceeded the standard
        deviation, and printed "clears noise" beside a worst seed that lost
        twelve thousand dollars. With five seeds a standard deviation is a
        rough number; a seed that loses outright is not.
        """
        mean, _, worst = self.margin_over(baseline, capacity)
        return mean > 0 and worst >= 0

    def verdict_against(self, baseline: str, capacity: int) -> str:
        mean, _, _ = self.margin_over(baseline, capacity)
        if self.never_loses_to(baseline, capacity):
            return "never loses"
        if mean > 0:
            return "ahead on average, LOSES on at least one seed"
        return "NOT AHEAD on average"

    def share_of_oracle(self, policy: str, capacity: int) -> float:
        """What fraction of the achievable value this policy captured."""
        captured = float(np.mean([run.recovered_at(policy, capacity) for run in self.runs]))
        ceiling = float(np.mean([run.recovered_at("oracle", capacity) for run in self.runs]))
        return captured / ceiling if ceiling else 0.0

    def model_earns_its_complexity(self) -> tuple[bool, str]:
        """Is the gradient booster better than the logistic regression?

        Asked explicitly and answered honestly. Reporting only the winner is
        how a project ends up claiming a model earns its keep when a linear
        fit would have done, and an interviewer who asks this question and
        gets a vague answer has learned something unflattering.
        """
        boosted_brier = float(np.mean([r.model_report.brier for r in self.runs]))
        linear_brier = float(np.mean([r.baseline_report.brier for r in self.runs]))
        boosted_auc = float(np.mean([r.model_report.auc for r in self.runs]))
        linear_auc = float(np.mean([r.baseline_report.auc for r in self.runs]))

        better_brier = boosted_brier < linear_brier
        better_auc = boosted_auc > linear_auc

        if better_brier and better_auc:
            return True, (
                "gradient boosting wins on both calibration and ranking. The queue is still "
                "ranked with the logistic regression by fixed rule; switching should rest on "
                "a margin that holds on data this report was not built from"
            )
        if not better_brier and not better_auc:
            return False, (
                "the logistic regression matches or beats it on both measures - the "
                "gradient booster is not earning its complexity on this data. The queue is "
                "ranked with the logistic regression"
            )
        winner = "calibration (Brier)" if better_brier else "ranking (AUC)"
        loser = "ranking (AUC)" if better_brier else "calibration (Brier)"
        return False, (
            f"mixed: gradient boosting is better on {winner} and worse on {loser}, by "
            f"margins too small to call. On fifteen features and a few thousand rows, "
            f"that is the expected result. The queue is ranked with the logistic regression, "
            f"which is the defensible choice"
        )

    def summary_lines(self) -> list[str]:
        lines = [
            f"Evaluated over {len(self.runs)} seeds on held-out cases.",
            "",
            f"{'capacity':>9}  {'arrival':>12}  {'amount':>12}  {'model':>12}  {'oracle':>12}",
        ]
        for capacity in self.capacities:
            cells = []
            for policy in ("arrival", "amount", "model", "oracle"):
                values = [run.recovered_at(policy, capacity) / 100.0 for run in self.runs]
                cells.append(f"${np.mean(values):>11,.0f}")
            lines.append(f"{capacity:>9}  " + "  ".join(cells))

        lines += ["", "Model quality, mean across seeds:"]
        lines.append(
            f"  gradient boosting  Brier {np.mean([r.model_report.brier for r in self.runs]):.4f}"
            f"   AUC {np.mean([r.model_report.auc for r in self.runs]):.3f}"
        )
        lines.append(
            f"  logistic baseline  Brier "
            f"{np.mean([r.baseline_report.brier for r in self.runs]):.4f}"
            f"   AUC {np.mean([r.baseline_report.auc for r in self.runs]):.3f}"
        )
        return lines


def run_one_seed(
    seed: int,
    *,
    arm: ArmSpec = INFLATED_ARM,
    capacities: tuple[int, ...] = DEFAULT_CAPACITY_SWEEP,
    payments_cache: dict | None = None,
) -> SeedRun:
    """Generate, fit, and score one seed end to end."""
    # The payment ledger is deterministic given the arm, so it is generated
    # once and reused. Only the case layer varies by seed, which is where the
    # interesting variation lives anyway.
    if payments_cache is not None and arm.name in payments_cache:
        payments = payments_cache[arm.name]
    else:
        _, payments, _ = build_arm(arm)
        if payments_cache is not None:
            payments_cache[arm.name] = payments

    cases, _ = generate_cases(
        payments,
        rng=np.random.default_rng(seed),
        window_start=WINDOW_START,
        window_days=arm.window_days,
    )
    train, test = temporal_split(cases)

    model = RecoverabilityModel(kind="boosted").fit(train)
    baseline = RecoverabilityModel(kind="linear").fit(train)

    # The queue is ranked by the logistic regression, as a fixed rule rather
    # than whichever model scores better on this run's test split - picking
    # the winner on test data would flatter the report. The rule followed
    # earlier runs in which the booster did not earn its complexity; the
    # second review found the report recommending the linear model while the
    # harness quietly ranked with the booster.
    ranker = baseline
    probabilities = ranker.predict_proba(test)
    actual = np.array([c.recovered for c in test], dtype=bool)

    return SeedRun(
        seed=seed,
        model_report=model.evaluate(test, n_train=len(train)),
        baseline_report=baseline.evaluate(test, n_train=len(train)),
        results=sweep(test, probabilities, capacities=capacities),
        calibration=calibration_table(probabilities, actual),
    )


def evaluate(
    *,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    arm: ArmSpec = INFLATED_ARM,
    capacities: tuple[int, ...] = DEFAULT_CAPACITY_SWEEP,
) -> Evaluation:
    """The full evaluation."""
    cache: dict = {}
    runs = [run_one_seed(s, arm=arm, capacities=capacities, payments_cache=cache) for s in seeds]
    return Evaluation(runs=runs, capacities=capacities)


def render_report(evaluation: Evaluation) -> str:
    """The human-readable report, with its own caveats attached.

    The caveats are part of the artifact rather than a covering note, because
    a table of dollar figures gets screenshotted and the note does not travel
    with it.
    """
    lines = list(evaluation.summary_lines())

    earns_it, verdict = evaluation.model_earns_its_complexity()
    lines += [
        "",
        "The 'model' column is the logistic regression; the booster is scored only.",
        f"Does the gradient booster earn its complexity? {'Yes' if earns_it else 'No'}.",
        f"  {verdict}.",
    ]

    lines += [
        "",
        "Advantage over largest-amount-first, in dollars recovered:",
        "  (the real competitor - it needs no model, no training data, no maintenance)",
    ]
    for capacity in evaluation.capacities:
        mean, spread, worst = evaluation.margin_over("amount", capacity)
        verdict_text = evaluation.verdict_against("amount", capacity)
        lines.append(
            f"  capacity {capacity:>3}: ${mean:>+9,.0f}  (sd ${spread:,.0f}, "
            f"worst seed ${worst:+,.0f}) - {verdict_text}"
        )

    lines += ["", "Share of the achievable ceiling captured:"]
    for capacity in evaluation.capacities:
        lines.append(
            f"  capacity {capacity:>3}: model {evaluation.share_of_oracle('model', capacity):>5.1%}"
            f"   amount {evaluation.share_of_oracle('amount', capacity):>5.1%}"
            f"   arrival {evaluation.share_of_oracle('arrival', capacity):>5.1%}"
        )

    lines += [
        "",
        "On the comparison against arrival order",
        "-" * 39,
        "Arrival order recovers so little at low capacity that a ratio against it runs",
        "to three figures. That is arithmetically true and worth nothing: the",
        "denominator is noise. The absolute dollar figures in the table above are the",
        "honest form of that comparison, and the margin against largest-amount-first is",
        "the one that actually has to be earned.",
        "",
    ]

    lines += [
        "",
        "What these numbers are not",
        "-" * 26,
        "Synthetic data. The recoverability curve is calibrated from UK mule-account",
        "data because no US equivalent was found to exist, and no real institution's",
        "behaviour appears anywhere in it. None of these figures transfers to",
        "production, and quoting them as though they did would misrepresent the work.",
        "",
        "What they do show is that the decision architecture is measurable: every",
        "margin above is reported with its worst seed, and the gap to a",
        "perfect-knowledge oracle is visible rather than hidden.",
    ]
    losing = [c for c in evaluation.capacities if not evaluation.never_loses_to("amount", c)]
    lines += [
        "",
        (
            "Against largest-amount-first the model never lost a seed at any capacity."
            if not losing
            else "Against largest-amount-first the model lost at least one seed at capacity "
            + ", ".join(str(c) for c in losing)
            + ". Where that happens, ranking by amount alone is a reasonable choice and "
            "the model has not shown it should replace it."
        ),
    ]
    return "\n".join(lines)
