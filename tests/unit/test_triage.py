"""M6: does ranking beat not ranking, and is the model honest about itself?

The most important test in this file is the leakage one. Three fields on a
generated case are only knowable after the case is worked, and a model given
any of them scores beautifully and cannot be used. That failure is invisible
in every metric, so it has to be caught structurally.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from interlock.generator.config import INFLATED_ARM
from interlock.generator.recall_cases import generate_cases
from interlock.generator.testbed import WINDOW_START, build_arm
from interlock.triage.features import (
    FEATURE_NAMES,
    FORBIDDEN_FEATURES,
    feature_matrix,
    features_for,
    temporal_split,
)
from interlock.triage.harness import Evaluation, evaluate, render_report, run_one_seed
from interlock.triage.model import RecoverabilityModel, calibration_table
from interlock.triage.policy import POLICIES, run_policy, sweep


@pytest.fixture(scope="module")
def cases():
    _, payments, _ = build_arm(INFLATED_ARM)
    generated, _ = generate_cases(
        payments,
        rng=np.random.default_rng(4242),
        window_start=WINDOW_START,
        window_days=INFLATED_ARM.window_days,
    )
    return generated


@pytest.fixture(scope="module")
def evaluation() -> Evaluation:
    return evaluate(seeds=(11, 23, 37))


class TestNoLeakage:
    """The easiest mistake in the project, and the one metrics cannot show.

    A review replaced a legitimate feature with ``share_remaining`` - a
    textbook post-outcome leak - and all twenty-five tests here passed. The
    name-based check could not see it because the emitted column was not named
    after the field it read, and the perfect-separation check could not either
    because a noisy continuous leak does not separate a Bernoulli label
    cleanly. These tests attack the guard rather than restate it.
    """

    def test_reading_a_forbidden_field_is_refused_at_runtime(
        self, monkeypatch: pytest.MonkeyPatch, cases
    ) -> None:
        """The test the old suite was missing.

        Patch ``features_for`` to read a post-outcome field and assert the
        matrix refuses to build. Previously this produced a working model with
        an excellent score.
        """
        import interlock.triage.features as feat

        def leaky(case, history=None):
            row = [0.0] * len(feat.FEATURE_NAMES)
            row[6] = case.share_remaining  # a forbidden field, read directly
            return row

        monkeypatch.setattr(feat, "features_for", leaky)
        with pytest.raises(feat.LeakageError, match="share_remaining"):
            feat.feature_matrix(cases[:10])

    def test_the_guard_names_which_field_leaked(
        self, monkeypatch: pytest.MonkeyPatch, cases
    ) -> None:
        import interlock.triage.features as feat

        def leaky(case, history=None):
            return [float(case.responded_after_hours)] * len(feat.FEATURE_NAMES)

        monkeypatch.setattr(feat, "features_for", leaky)
        with pytest.raises(feat.LeakageError, match="responded_after_hours"):
            feat.feature_matrix(cases[:10])

    def test_a_leak_through_getattr_is_refused(self, monkeypatch, cases) -> None:
        """The text-search guard this replaced could not see this."""
        import interlock.triage.features as feat

        field = "share" + "_remaining"

        def leaky(case, known=None):
            return [float(getattr(case, field))] * len(feat.FEATURE_NAMES)

        monkeypatch.setattr(feat, "features_for", leaky)
        with pytest.raises(feat.LeakageError, match="share_remaining"):
            feat.feature_matrix(cases[:10])

    def test_a_leak_through_a_helper_is_refused(self, monkeypatch, cases) -> None:
        import interlock.triage.features as feat

        def innocent_looking(case):
            return float(case.recovered)

        def leaky(case, known=None):
            return [innocent_looking(case)] * len(feat.FEATURE_NAMES)

        monkeypatch.setattr(feat, "features_for", leaky)
        with pytest.raises(feat.LeakageError, match="recovered"):
            feat.feature_matrix(cases[:10])

    @pytest.mark.parametrize(
        "latent",
        ["counterparty_return_willingness", "counterparty_answers_within_window", "scam_category"],
    )
    def test_generator_parameters_are_refused_too(self, monkeypatch, cases, latent) -> None:
        """The subtler leak that survived the first review: the true parameter."""
        import interlock.triage.features as feat

        def leaky(case, known=None):
            _ = getattr(case, latent)
            return [0.0] * len(feat.FEATURE_NAMES)

        monkeypatch.setattr(feat, "features_for", leaky)
        with pytest.raises(feat.LeakageError, match="generator's own parameter"):
            feat.feature_matrix(cases[:10])

    @pytest.mark.parametrize(
        "escape",
        [
            lambda v: v.__replace__(),
            lambda v: v.__getstate__(),
            lambda v: v.__reduce_ex__(4),
            lambda v: __import__("pickle").dumps(v),
            lambda v: __import__("copy").copy(v),
        ],
    )
    def test_the_view_cannot_be_unwrapped_by_dunders(self, cases, escape) -> None:
        """Third review: __replace__ returned the whole unguarded case."""
        from interlock.triage.features import LeakageError, RankingTimeView

        with pytest.raises(LeakageError):
            escape(RankingTimeView(cases[0]))

    def test_the_real_feature_code_passes_the_guard(self, cases) -> None:
        x, _, _ = feature_matrix(cases[:50], cases)
        assert x.shape == (50, len(FEATURE_NAMES))


class TestCounterpartyHistoryRespectsTime:
    def test_a_row_never_sees_its_own_label(self, cases) -> None:
        """The second review's finding: fit rows were scored on estimates that knew the answer."""
        from interlock.triage.features import feature_matrix

        train, _ = temporal_split(cases)
        rows = train[:400]
        x_before, _, _ = feature_matrix(rows, rows)

        target = 123
        flipped = replace(rows[target], recovered=not rows[target].recovered)
        changed = [*rows[:target], flipped, *rows[target + 1 :]]
        x_after, _, _ = feature_matrix(changed, changed)

        assert np.array_equal(x_before[target], x_after[target])

    def test_only_cases_closed_before_arrival_count(self, cases) -> None:
        from interlock.triage.features import closed_at, history_as_of

        train, _ = temporal_split(cases)
        pool = train[:600]
        row = pool[-1]
        known = history_as_of([row], pool)[0]
        eligible = [
            c
            for c in pool
            if c.requesting_institution_id == row.requesting_institution_id
            and closed_at(c) < row.received_at
        ]
        if not eligible:
            assert known is None
        else:
            assert known is not None
            assert known.closed_cases == len(eligible)

    def test_estimates_are_not_the_generator_parameter(self, cases) -> None:
        from interlock.triage.features import counterparty_history

        train, _ = temporal_split(cases)
        history = counterparty_history(train)
        sample = next(c for c in train if c.requesting_institution_id in history)
        assert history[sample.requesting_institution_id].recovery_rate != pytest.approx(
            sample.counterparty_return_willingness
        )

    def test_an_unknown_counterparty_gets_the_population_prior(self, cases) -> None:
        """A cold start is the first weeks of any real deployment."""
        from interlock.triage.features import POPULATION_PRIOR_RECOVERY, features_for

        row = features_for(cases[0], None)
        assert row[6] == pytest.approx(POPULATION_PRIOR_RECOVERY)
        assert row[7] == 0.0, "an unknown counterparty must not read as established"

    def test_the_first_arrival_has_no_history(self, cases) -> None:
        from interlock.triage.features import history_as_of

        earliest = min(cases, key=lambda c: c.received_at)
        assert history_as_of([earliest], cases)[0] is None


class TestFeatureShape:
    def test_no_post_outcome_field_is_named_in_the_feature_set(self) -> None:
        for forbidden in FORBIDDEN_FEATURES:
            assert forbidden not in FEATURE_NAMES

    def test_features_do_not_reconstruct_the_outcome(self, cases) -> None:
        """Weaker than it looks - kept because it costs nothing, not because
        it is sufficient. A noisy leak passes it, which is why the runtime
        guard above exists."""
        x, y, _ = feature_matrix(cases)
        for index, name in enumerate(FEATURE_NAMES):
            column = x[:, index]
            if len(np.unique(column)) < 2:
                continue
            positives, negatives = column[y], column[~y]
            if len(positives) == 0 or len(negatives) == 0:
                continue
            separated = positives.min() > negatives.max() or positives.max() < negatives.min()
            assert not separated, f"{name} perfectly separates the label - suspect a leak"

    def test_feature_names_match_what_is_emitted(self, cases) -> None:
        assert len(features_for(cases[0])) == len(FEATURE_NAMES)
        x, _, _ = feature_matrix(cases)
        assert x.shape[1] == len(FEATURE_NAMES)

    def test_value_is_returned_separately_from_features(self, cases) -> None:
        """Amount is a feature; what a recovery is worth is not the model's."""
        x, y, value = feature_matrix(cases)
        assert len(value) == len(y) == x.shape[0]


class TestTemporalDiscipline:
    def test_split_is_taken_from_the_generator(self, cases) -> None:
        train, test = temporal_split(cases)
        assert train and test
        assert max(c.received_at for c in train) <= min(c.received_at for c in test)

    def test_model_fits_without_seeing_test_cases(self, cases) -> None:
        train, test = temporal_split(cases)
        model = RecoverabilityModel().fit(train)
        probabilities = model.predict_proba(test)
        assert len(probabilities) == len(test)
        assert np.all((probabilities >= 0) & (probabilities <= 1))


class TestModelQuality:
    def test_probabilities_are_roughly_calibrated(self, cases) -> None:
        """The crudest calibration check: mean prediction near the base rate."""
        train, test = temporal_split(cases)
        model = RecoverabilityModel().fit(train)
        predicted = float(np.mean(model.predict_proba(test)))
        actual = float(np.mean([c.recovered for c in test]))
        assert abs(predicted - actual) < 0.10, (
            f"predicts {predicted:.1%} on a population that recovers {actual:.1%}"
        )

    def test_calibration_table_is_produced(self, cases) -> None:
        train, test = temporal_split(cases)
        model = RecoverabilityModel().fit(train)
        table = calibration_table(model.predict_proba(test), np.array([c.recovered for c in test]))
        assert table
        assert all({"mean_predicted", "observed_rate", "count"} <= set(r) for r in table)

    def test_both_model_kinds_fit(self, cases) -> None:
        train, test = temporal_split(cases)
        for kind in ("boosted", "linear"):
            report = RecoverabilityModel(kind=kind).fit(train).evaluate(test, n_train=len(train))
            assert 0.0 <= report.brier <= 1.0
            assert 0.0 <= report.auc <= 1.0

    def test_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown model kind"):
            RecoverabilityModel(kind="neural")

    def test_a_single_class_split_degrades_to_the_base_rate(self) -> None:
        """Rather than raising mid-sweep."""
        model = RecoverabilityModel().fit([])
        assert len(model.predict_proba([])) == 0


class TestPolicies:
    def test_every_policy_runs(self, cases) -> None:
        _, test = temporal_split(cases)
        probabilities = np.full(len(test), 0.5)
        for policy in POLICIES:
            result = run_policy(test, probabilities, policy=policy, capacity=20)
            assert result.cases_worked == min(20, len(test))
            assert result.recovered_cents >= 0

    def test_unknown_policy_is_refused(self, cases) -> None:
        with pytest.raises(ValueError, match="unknown policy"):
            run_policy(cases, np.zeros(len(cases)), policy="vibes", capacity=5)

    def test_oracle_is_the_ceiling(self, cases) -> None:
        """No policy may beat perfect knowledge. If one does, the evaluation
        is scoring something other than what it claims."""
        _, test = temporal_split(cases)
        probabilities = np.random.default_rng(1).random(len(test))
        results = {r.policy: r for r in sweep(test, probabilities, capacities=(25,))}
        ceiling = results["oracle"].recovered_cents
        for policy, result in results.items():
            assert result.recovered_cents <= ceiling, f"{policy} beat the oracle"

    def test_capacity_is_respected(self, cases) -> None:
        _, test = temporal_split(cases)
        result = run_policy(test, np.zeros(len(test)), policy="amount", capacity=7)
        assert result.cases_worked == 7

    def test_ties_break_deterministically(self, cases) -> None:
        """Identical scores must not depend on list order."""
        _, test = temporal_split(cases)
        flat = np.full(len(test), 0.5)
        first = run_policy(test, flat, policy="model", capacity=15)
        second = run_policy(list(test), flat, policy="model", capacity=15)
        assert first.recovered_cents == second.recovered_cents


class TestTheCentralClaim:
    def test_model_beats_amount_first_at_scarce_capacity(self, evaluation: Evaluation) -> None:
        """Where ranking should matter most.

        Largest-first is the real competitor: no model, no training data, no
        maintenance. Beating it at low capacity is the claim that has to hold.
        """
        assert evaluation.never_loses_to("amount", 5), (
            "at capacity 5 the model must beat largest-first on every seed"
        )

    def test_model_beats_arrival_order_everywhere(self, evaluation: Evaluation) -> None:
        for capacity in evaluation.capacities:
            mean, _, _ = evaluation.margin_over("arrival", capacity)
            assert mean > 0, f"model lost to arrival order at capacity {capacity}"

    def test_ranking_matters_less_as_capacity_grows(self, evaluation: Evaluation) -> None:
        """The shape that has to hold, or the premise is wrong.

        With enough capacity to work everything, which cases you pick first
        stops mattering. A model whose advantage grew with capacity would be
        measuring something other than triage.
        """
        low, _, _ = evaluation.margin_over("arrival", 5)
        high, _, _ = evaluation.margin_over("arrival", 100)
        low_share = evaluation.share_of_oracle("model", 5)
        high_share = evaluation.share_of_oracle("amount", 100)
        assert high_share > 0.5, "at high capacity even a crude policy captures most value"
        assert low_share > evaluation.share_of_oracle("amount", 5), (
            "at scarce capacity the model must capture far more of the ceiling"
        )
        assert low > 0 and high > 0

    def test_no_policy_captures_the_whole_ceiling(self, evaluation: Evaluation) -> None:
        """If the model matched the oracle, the problem would be trivial and
        the data would be wrong."""
        assert evaluation.share_of_oracle("model", 20) < 0.95


class TestReportingIsHonest:
    def test_the_booster_is_compared_to_the_linear_baseline(self, evaluation: Evaluation) -> None:
        earns_it, verdict = evaluation.model_earns_its_complexity()
        assert isinstance(earns_it, bool)
        assert verdict, "the comparison must produce a stated verdict either way"

    def test_report_states_the_numbers_do_not_transfer(self, evaluation: Evaluation) -> None:
        report = render_report(evaluation)
        assert "Synthetic data" in report
        assert "no US equivalent" in report
        assert "transfers to" in report

    def test_report_explains_why_the_arrival_ratio_is_not_quoted(
        self, evaluation: Evaluation
    ) -> None:
        """An earlier version quoted 200x uplift over arrival order, which is
        true and meaningless - the denominator is noise."""
        report = render_report(evaluation)
        assert "denominator is noise" in report

    def test_a_losing_seed_is_never_called_a_win(self) -> None:
        """The second review: "clears noise" printed beside a worst seed of -$12,077."""
        from interlock.triage.harness import SeedRun
        from interlock.triage.policy import PolicyResult

        def run(seed: int, model: int, amount: int) -> SeedRun:
            results = [
                PolicyResult(
                    policy=p, capacity=5, recovered_cents=v, cases_worked=5, recovered_count=1
                )
                for p, v in (
                    ("model", model),
                    ("amount", amount),
                    ("arrival", 0),
                    ("oracle", 10**9),
                )
            ]
            return SeedRun(seed=seed, model_report=None, baseline_report=None, results=results)

        big_win_one_loss = Evaluation(
            runs=[run(1, 10_000_000, 0), run(2, 10_000_000, 0), run(3, 0, 1_000_000)],
            capacities=(5,),
        )
        assert not big_win_one_loss.never_loses_to("amount", 5)
        assert "LOSES on at least one seed" in big_win_one_loss.verdict_against("amount", 5)

    def test_report_says_which_model_ranks(self, evaluation: Evaluation) -> None:
        report = render_report(evaluation)
        assert "'model' column is the logistic regression" in report

    def test_report_shows_the_oracle_gap(self, evaluation: Evaluation) -> None:
        assert "ceiling captured" in render_report(evaluation)

    def test_a_single_seed_run_is_self_contained(self) -> None:
        run = run_one_seed(5, capacities=(10,))
        assert run.model_report.n_test > 0
        assert run.recovered_at("model", 10) >= 0
        assert run.calibration
