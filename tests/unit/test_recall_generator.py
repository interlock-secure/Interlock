"""M5: does the generated case population teach anything real?

The acceptance criteria that matter are the negative ones. It is easy to
generate recall cases; it is easy to generate ones where a single threshold on
elapsed time predicts the outcome perfectly, and a model trained on those has
learned the generator rather than the problem.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from interlock.generator.config import INFLATED_ARM
from interlock.generator.counterparties import COUNTERPARTY_COUNT, build_counterparties
from interlock.generator.recall_cases import (
    GeneratedCase,
    case_statistics,
    generate_cases,
)
from interlock.generator.recovery import (
    RESIDUAL_FLOOR,
    RUSI_ANCHORS,
    expected_share_remaining,
    sample_outcome,
)
from interlock.generator.testbed import WINDOW_START, build_arm

SEED = 20260919


@pytest.fixture(scope="module")
def generated() -> tuple[list[GeneratedCase], dict]:
    _, payments, _ = build_arm(INFLATED_ARM)
    rng = np.random.default_rng(SEED)
    cases, _counterparties = generate_cases(
        payments,
        rng=rng,
        window_start=WINDOW_START,
        window_days=INFLATED_ARM.window_days,
    )
    return cases, case_statistics(cases)


class TestTheCurveMatchesItsPublishedAnchors:
    """If this drifts, every downstream number rests on a different curve."""

    @pytest.mark.parametrize(("minutes", "expected"), RUSI_ANCHORS)
    def test_anchor(self, minutes: float, expected: float) -> None:
        actual = expected_share_remaining(minutes)
        assert actual == pytest.approx(expected, abs=0.06), (
            f"At {minutes} minutes the RUSI data puts {expected:.0%} remaining; "
            f"the model says {actual:.0%}"
        )

    def test_curve_is_monotonic(self) -> None:
        shares = [expected_share_remaining(m) for m in range(0, 2880, 15)]
        assert all(a >= b for a, b in pairwise(shares))

    def test_curve_never_reaches_zero(self) -> None:
        """Some funds never move. A curve hitting zero makes every old case
        perfectly predictable, which is both wrong and too easy."""
        assert expected_share_remaining(100_000) >= RESIDUAL_FLOOR * 0.99

    def test_curve_starts_whole(self) -> None:
        assert expected_share_remaining(0) == 1.0


class TestNoTrivialRuleWins:
    """The M2 lesson, applied to outcomes instead of accounts."""

    def test_no_single_feature_threshold_beats_f1_075(
        self, generated: tuple[list[GeneratedCase], dict]
    ) -> None:
        cases, _ = generated
        labels = np.array([c.recovered for c in cases])

        features = {
            "minutes_since_settlement": np.array([c.minutes_since_settlement for c in cases]),
            "amount_cents": np.array([float(c.amount_cents) for c in cases]),
            "counterparty_median_response_hours": np.array(
                [c.counterparty_median_response_hours for c in cases]
            ),
            "counterparty_return_willingness": np.array(
                [c.counterparty_return_willingness for c in cases]
            ),
        }

        worst: tuple[str, float] = ("", 0.0)

        for name, values in features.items():
            for q in np.linspace(0.02, 0.98, 49):
                threshold = float(np.quantile(values, q))
                for predicted in (values <= threshold, values > threshold):
                    tp = int(np.sum(predicted & labels))
                    fp = int(np.sum(predicted & ~labels))
                    fn = int(np.sum(~predicted & labels))
                    if tp == 0:
                        continue
                    precision = tp / (tp + fp)
                    recall = tp / (tp + fn)
                    f1 = 2 * precision * recall / (precision + recall)
                    if f1 > worst[1]:
                        worst = (f"{name} @ {threshold:.4g}", f1)

        assert worst[1] <= 0.75, (
            f"A single threshold on {worst[0]} scores F1 {worst[1]:.3f}. The generator is "
            f"too clean - add variation rather than weakening the detector."
        )

    def test_elapsed_time_is_informative_but_not_sufficient(
        self, generated: tuple[list[GeneratedCase], dict]
    ) -> None:
        """Both halves matter.

        If elapsed time carried no signal the whole triage premise would be
        wrong; if it carried all of it there would be nothing to model.
        """
        cases, _ = generated
        recovered = [c.minutes_since_settlement for c in cases if c.recovered]
        lost = [c.minutes_since_settlement for c in cases if not c.recovered]

        assert recovered and lost
        assert np.median(recovered) < np.median(lost), "elapsed time must carry signal"

        # And the distributions must overlap substantially.
        overlap = sum(1 for m in lost if m < np.percentile(recovered, 75))
        assert overlap / len(lost) > 0.15, "the classes are too cleanly separated by time"


class TestCounterpartiesAreNotClones:
    def test_each_draws_its_own_parameters(self) -> None:
        rng = np.random.default_rng(SEED)
        parties = build_counterparties(rng)
        assert len(parties) == COUNTERPARTY_COUNT

        medians = [p.median_response_hours for p in parties]
        assert len(set(medians)) == len(medians), "no two institutions share a response time"
        assert max(medians) / min(medians) > 5, "the spread is too narrow to matter"

    def test_speed_and_willingness_are_separate_behaviours(self) -> None:
        """An institution can be eager and slow, or prompt and unhelpful."""
        rng = np.random.default_rng(SEED)
        parties = build_counterparties(rng)
        speed = np.array([p.median_response_hours for p in parties])
        willingness = np.array([p.return_willingness for p in parties])
        correlation = float(np.corrcoef(speed, willingness)[0, 1])
        assert abs(correlation) < 0.4, (
            f"response speed and willingness correlate at {correlation:.2f}; they would "
            f"collapse into one feature"
        )


class TestSplitsAreTemporal:
    def test_no_train_case_postdates_any_test_case(
        self, generated: tuple[list[GeneratedCase], dict]
    ) -> None:
        cases, _ = generated
        train = [c.received_at for c in cases if c.split == "train"]
        test = [c.received_at for c in cases if c.split == "test"]
        assert train and test
        assert max(train) <= min(test), (
            "a random split would let the model see an institution's later behaviour while "
            "predicting its earlier cases"
        )

    def test_both_splits_are_substantial(self, generated: tuple[list[GeneratedCase], dict]) -> None:
        _, stats = generated
        assert stats["train"] > 100
        assert stats["test"] > 40


class TestPopulationShape:
    def test_cases_are_produced(self, generated: tuple[list[GeneratedCase], dict]) -> None:
        _, stats = generated
        assert stats["total"] > 200

    def test_not_every_fraud_becomes_a_case(self) -> None:
        """Roughly a quarter of victims never report, so those payments make
        no case at all. A generator that produced one case per fraud would
        overstate the queue by about a third."""
        _, payments, _ = build_arm(INFLATED_ARM)
        cross_fraud = sum(1 for p in payments if p.is_fraud and p.is_cross_institution)
        cases, _ = generate_cases(
            payments,
            rng=np.random.default_rng(SEED),
            window_start=WINDOW_START,
            window_days=INFLATED_ARM.window_days,
        )
        assert len(cases) < cross_fraud * 0.95

    def test_recovery_rate_is_plausible(self, generated: tuple[list[GeneratedCase], dict]) -> None:
        """Not calibrated to anything published - no US recovery rate exists -
        but a rate near 0 or near 1 would mean the model is degenerate."""
        _, stats = generated
        assert 0.05 < stats["recovery_rate"] < 0.80

    def test_both_channels_appear(self, generated: tuple[list[GeneratedCase], dict]) -> None:
        _, stats = generated
        assert 0.15 < stats["structured_share"] < 0.90, (
            "email intake must be a real share of the queue, not a rounding error"
        )

    def test_several_rails_appear(self, generated: tuple[list[GeneratedCase], dict]) -> None:
        _, stats = generated
        assert len(stats["by_rail"]) >= 2


class TestOutcomeSampling:
    def test_identical_inputs_do_not_give_identical_outcomes(self) -> None:
        rng = np.random.default_rng(7)
        shares = {
            sample_outcome(
                rng,
                amount_cents=500_000,
                minutes_elapsed=45.0,
                operator_speed=1.0,
                institution_responsiveness=0.5,
                return_willingness=0.5,
            ).share_remaining
            for _ in range(50)
        }
        assert len(shares) > 40, "real recoveries turn on who picked up the phone"

    def test_a_sub_dollar_recovery_is_not_a_success(self) -> None:
        rng = np.random.default_rng(3)
        outcome = sample_outcome(
            rng,
            amount_cents=10,
            minutes_elapsed=100_000.0,
            operator_speed=5.0,
            institution_responsiveness=0.0,
            return_willingness=1.0,
        )
        assert not outcome.recovered
        assert outcome.recovered_cents == 0

    def test_recovered_amount_never_exceeds_the_payment(self) -> None:
        rng = np.random.default_rng(11)
        for _ in range(500):
            outcome = sample_outcome(
                rng,
                amount_cents=250_000,
                minutes_elapsed=float(rng.uniform(0, 5000)),
                operator_speed=float(rng.uniform(0.2, 4.0)),
                institution_responsiveness=float(rng.random()),
                return_willingness=float(rng.random()),
            )
            assert 0 <= outcome.recovered_cents <= 250_000


class TestDeterminism:
    def test_the_same_seed_gives_the_same_cases(self) -> None:
        _, payments, _ = build_arm(INFLATED_ARM)
        first, _ = generate_cases(
            payments,
            rng=np.random.default_rng(99),
            window_start=WINDOW_START,
            window_days=INFLATED_ARM.window_days,
        )
        second, _ = generate_cases(
            payments,
            rng=np.random.default_rng(99),
            window_start=WINDOW_START,
            window_days=INFLATED_ARM.window_days,
        )
        assert [c.case_id for c in first] == [c.case_id for c in second]
        assert [c.recovered for c in first] == [c.recovered for c in second]


class TestAssumptionsAreLabelled:
    def test_every_recovery_constant_declares_its_provenance(self) -> None:
        """A UK-derived number presented as a US one is the failure mode."""
        import inspect

        from interlock.generator import recovery

        # Whitespace-normalised: the claim has to be documented, not wrapped
        # in any particular way. An earlier version of this test failed
        # because the sentence happened to break across a line.
        source = " ".join(inspect.getsource(recovery).split())
        assert source.count("[UK-DERIVED ASSUMPTION]") >= 3
        assert "RUSI" in source
        assert "no US equivalent was found to exist" in source
        assert "Lloyds" in source


def test_time_to_request_spans_orders_of_magnitude(
    generated: tuple[list[GeneratedCase], dict],
) -> None:
    """Three separate delays, not one.

    An impersonation scam unravels in hours; an invoice redirection can take
    weeks. Collapsing them would make the queue far too uniform.
    """
    cases, _ = generated
    minutes = np.array([c.minutes_since_settlement for c in cases])
    assert np.percentile(minutes, 5) < 6 * 60, "some victims notice within hours"
    assert np.percentile(minutes, 95) > 24 * 60, "some take more than a day"
    assert np.percentile(minutes, 99) > 7 * 24 * 60, "invoice redirection takes weeks"
