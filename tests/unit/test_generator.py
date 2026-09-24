"""Testbed generator: reproducibility, calibration, and difficulty.

The last of those is the one that matters most and the one most synthetic
fraud datasets skip. If mules are generated with an obvious signature and then
"detected" by looking for that signature, the benchmark measures nothing
except that the detector read the generator.

So several tests here assert that the data is *hard*: that a naive rule does
not separate the classes, that the confound population genuinely overlaps the
mule population, and that the evasion cohort actually evades.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from interlock.generator.config import (
    INFLATED_ARM,
    REALISTIC_ARM,
    TARGET_MEAN_FRAUD_RANGE_CENTS,
    AccountType,
    GeneratorSettings,
)
from interlock.generator.population import build_population, summarise
from interlock.generator.testbed import (
    WINDOW_START,
    build_arm,
    payment_statistics,
)


@pytest.fixture(scope="module")
def realistic():
    accounts, payments, settings = build_arm(REALISTIC_ARM)
    return accounts, payments, settings


@pytest.fixture(scope="module")
def inflated():
    accounts, payments, settings = build_arm(INFLATED_ARM)
    return accounts, payments, settings


class TestReproducibility:
    """A benchmark nobody can re-run is not evidence."""

    def test_population_is_deterministic(self):
        first = build_population(GeneratorSettings(), WINDOW_START)
        second = build_population(GeneratorSettings(), WINDOW_START)
        assert [a.account_id for a in first] == [a.account_id for a in second]
        assert [a.is_mule for a in first] == [a.is_mule for a in second]
        assert [a.opened_at for a in first] == [a.opened_at for a in second]

    def test_payments_are_deterministic(self):
        _, first, _ = build_arm(INFLATED_ARM)
        _, second, _ = build_arm(INFLATED_ARM)
        assert len(first) == len(second)
        assert [p.payment_id for p in first] == [p.payment_id for p in second]
        assert [p.amount_cents for p in first] == [p.amount_cents for p in second]

    def test_a_different_seed_produces_different_data(self):
        # Guards against a generator that ignores its seed, which would make
        # the determinism tests above vacuous.
        base = build_population(GeneratorSettings(seed=1), WINDOW_START)
        other = build_population(GeneratorSettings(seed=2), WINDOW_START)
        assert [a.opened_at for a in base] != [a.opened_at for a in other]

    def test_account_hashes_are_wire_valid(self, realistic):
        accounts, _, _ = realistic
        for account in accounts[:200]:
            assert len(account.account_hash) == 64
            assert all(c in "0123456789abcdef" for c in account.account_hash)


class TestCalibration:
    def test_fraud_rate_matches_the_arm(self, realistic, inflated):
        for accounts_payments, arm in ((realistic, REALISTIC_ARM), (inflated, INFLATED_ARM)):
            _, payments, _ = accounts_payments
            stats = payment_statistics(payments, arm)
            observed = stats["observed_fraud_rate"]
            # Within 20% of target - Poisson draws and window clipping move it.
            assert arm.fraud_rate * 0.8 <= observed <= arm.fraud_rate * 1.2, (
                f"{arm.name} arm: observed {observed:.5%} against target {arm.fraud_rate:.5%}"
            )

    def test_mean_fraud_amount_sits_between_the_published_anchors(self, realistic, inflated):
        low, high = TARGET_MEAN_FRAUD_RANGE_CENTS
        for accounts_payments, arm in ((realistic, REALISTIC_ARM), (inflated, INFLATED_ARM)):
            _, payments, _ = accounts_payments
            stats = payment_statistics(payments, arm)
            mean = stats["mean_fraud_amount_cents"]
            assert low <= mean <= high, (
                f"{arm.name} arm mean of {mean} cents falls outside the anchor range "
                f"({low}-{high}). For a log-normal the mean is exp(mu + sigma^2/2), not "
                "the median - check whether a profile's sigma moved."
            )

    def test_purchase_scams_dominate_by_count(self, inflated):
        # [PUBLISHED] UK Finance 2025: purchase scams are 71% of APP cases.
        _, payments, _ = inflated
        stats = payment_statistics(payments, INFLATED_ARM)
        by_category = stats["fraud_by_category"]
        total = sum(by_category.values())
        assert by_category["purchase"] / total > 0.6

    def test_investment_scams_dominate_by_value(self, inflated):
        # The inverse of the above, and the reason value and count are
        # reported separately in the benchmark: they disagree.
        _, payments, _ = inflated
        fraud = [p for p in payments if p.is_fraud]
        by_value: dict[str, int] = {}
        for payment in fraud:
            key = payment.scam_category or "unknown"
            by_value[key] = by_value.get(key, 0) + payment.amount_cents
        top = max(by_value, key=lambda k: by_value[k])
        assert top == "investment", (
            "Investment scams should carry the largest share of value despite being 6% "
            f"of cases; got {top}"
        )

    def test_realistic_arm_has_enough_positives_to_measure(self, realistic):
        _, payments, _ = realistic
        fraud = sum(1 for p in payments if p.is_fraud)
        # The reason the realistic arm runs 360 days rather than 90. Below a
        # couple of hundred positives its confidence intervals are too wide
        # to support any claim.
        assert fraud >= 200, (
            f"Only {fraud} fraudulent payments in the realistic arm. Either the window "
            "or the population needs to grow before this arm can bound a metric."
        )


class TestPopulation:
    def test_mules_concentrate_at_smaller_institutions(self, realistic):
        accounts, _, _ = realistic
        by_institution: dict[str, list[int]] = {}
        for account in accounts:
            by_institution.setdefault(account.institution_id, []).append(int(account.is_mule))

        large_rate = sum(by_institution["metro-savings-bank"]) / len(
            by_institution["metro-savings-bank"]
        )
        small_rates = [
            sum(v) / len(v) for k, v in by_institution.items() if k != "metro-savings-bank"
        ]
        assert min(small_rates) > large_rate, (
            "Mule rate at every credit union should exceed the large bank's - the "
            "exposure asymmetry is the point of the uneven institution sizes."
        )

    def test_every_fairness_segment_is_populated(self, realistic):
        accounts, _, _ = realistic
        segments = summarise(accounts)["by_fairness_segment"]
        for required in (
            "thin_file_new",
            "small_business",
            "community_collector",
            "remittance_corridor",
            "established_personal",
        ):
            assert segments.get(required, 0) > 0, f"segment {required} is empty"


class TestTheDataIsActuallyHard:
    """Tests that the testbed does not hand a detector the answer."""

    def test_account_tenure_alone_does_not_separate_the_classes(self, realistic):
        """A tenure threshold should catch classic mules and miss evasive ones.

        If a single tenure rule separated mules cleanly, every downstream
        metric would be an artefact of the generator rather than a measurement
        of anything.
        """
        accounts, _, _ = realistic
        threshold = timedelta(days=90)

        young_mules = sum(
            1 for a in accounts if a.is_mule and (WINDOW_START - a.opened_at) < threshold
        )
        old_mules = sum(
            1 for a in accounts if a.is_mule and (WINDOW_START - a.opened_at) >= threshold
        )
        young_legit = sum(
            1 for a in accounts if not a.is_mule and (WINDOW_START - a.opened_at) < threshold
        )

        assert old_mules > 0, "Every mule is young - a tenure rule would be perfect"
        assert young_legit > young_mules, (
            "Young legitimate accounts must outnumber young mules, or a tenure rule "
            "gets a precision it could never have in production"
        )

    def test_legitimate_accounts_also_sweep_funds_onward(self, realistic):
        """Onward movement must not be a mule-only signature.

        A community collector gathering contributions and a mule laundering
        proceeds produce the same shape. If only mules swept, the highest
        precision dimension in the schema would be free.
        """
        accounts, payments, _ = realistic
        by_id = {a.account_id: a for a in accounts}

        legit_sweeps = sum(
            1 for p in payments if p.is_onward_sweep and not by_id[p.sender_account_id].is_mule
        )
        assert legit_sweeps > 0, "No legitimate account sweeps - the signature is free"

    def test_mules_receive_ordinary_traffic_too(self, realistic):
        """A mule account whose entire history is fraud is not realistic.

        The evasive cohort is aged 200 to 900 days; an account that old with
        no legitimate history would be a giveaway an operator would never
        leave.
        """
        accounts, payments, _ = realistic
        mule_ids = {a.account_id for a in accounts if a.is_mule}

        legit_inbound_to_mules = sum(
            1 for p in payments if p.receiver_account_id in mule_ids and not p.is_fraud
        )
        assert legit_inbound_to_mules > 0, (
            "Mules receive only fraudulent payments, so their inbound history is a "
            "perfect label. Ordinary traffic must reach them too."
        )

    def test_confound_accounts_exist_in_volume(self, realistic):
        accounts, _, _ = realistic
        confounds = sum(1 for a in accounts if a.account_type.is_confound)
        mules = sum(1 for a in accounts if a.is_mule)
        assert confounds > mules * 5, (
            f"{confounds} confound accounts against {mules} mules. The lookalike "
            "population has to dominate, or false positives are too cheap to measure."
        )


class TestTemporalSplit:
    def test_split_is_temporal_not_random(self, inflated):
        _, payments, _ = inflated
        from interlock.generator.testbed import _split_for

        train_times = [
            p.timestamp
            for p in payments
            if _split_for(p.timestamp, WINDOW_START, INFLATED_ARM) == "train"
        ]
        test_times = [
            p.timestamp
            for p in payments
            if _split_for(p.timestamp, WINDOW_START, INFLATED_ARM) == "test"
        ]
        assert train_times and test_times
        assert max(train_times) <= min(test_times), (
            "Train and test windows overlap in time, which lets a model learn from an "
            "account's own future - a capability it will not have in production."
        )

    def test_both_splits_contain_fraud(self, inflated):
        _, payments, _ = inflated
        stats = payment_statistics(payments, INFLATED_ARM)
        for split in ("train", "test"):
            assert stats["by_split"][split]["fraud"] > 0


class TestLedgerIntegrity:
    def test_no_self_payments(self, realistic):
        _, payments, _ = realistic
        assert all(p.sender_account_id != p.receiver_account_id for p in payments)

    def test_amounts_are_positive_integers(self, realistic):
        _, payments, _ = realistic
        for payment in payments[:5000]:
            assert isinstance(payment.amount_cents, int)
            assert payment.amount_cents > 0

    def test_fraud_payments_always_carry_a_category(self, realistic):
        _, payments, _ = realistic
        for payment in payments:
            if payment.is_fraud:
                assert payment.scam_category, "A fraudulent payment with no category"
            else:
                assert payment.scam_category is None

    def test_fraud_always_targets_a_mule(self, realistic):
        accounts, payments, _ = realistic
        mule_ids = {a.account_id for a in accounts if a.is_mule}
        for payment in payments:
            if payment.is_fraud:
                assert payment.receiver_account_id in mule_ids

    def test_payments_are_time_ordered(self, realistic):
        _, payments, _ = realistic
        timestamps = [p.timestamp for p in payments]
        assert timestamps == sorted(timestamps)

    def test_most_payments_cross_an_institution_boundary(self, realistic):
        # Only cross-institution payments are visible to the network, so if
        # most traffic were on-us the testbed would exercise very little.
        _, payments, _ = realistic
        cross = sum(1 for p in payments if p.is_cross_institution)
        assert cross / len(payments) > 0.5


class TestNaiveBaselineDoesNotWin:
    """Run a naive rule and measure it.

    Every other test argues the data is hard. This one checks. A rule built
    from the two most obvious signals - a young account receiving a burst of
    first-time inbound payments - is what a first-pass detector looks like,
    and on a toy dataset it scores perfectly.

    It has scored perfectly here twice during development. The first time
    because mules were the only high-velocity accounts; the second because
    they were the only accounts that were both young and busy. Both were
    fixed by adding legitimate lookalikes, not by weakening the rule. These
    assertions exist so a third regression is caught rather than shipped.

    Runs on the inflated arm: per-account analysis needs the mule population
    that arm carries, and ArmSpec.supports says so.
    """

    @staticmethod
    def _naive_flags(accounts, payments, *, tenure_days: int, inbound_threshold: int):
        from collections import defaultdict

        by_id = {a.account_id: a for a in accounts}
        first_time_inbound: dict[str, int] = defaultdict(int)
        for payment in payments:
            if payment.is_first_time_payee:
                first_time_inbound[payment.receiver_account_id] += 1

        flagged = {
            a.account_id
            for a in accounts
            if (WINDOW_START - a.opened_at).days <= tenure_days
            and first_time_inbound[a.account_id] >= inbound_threshold
        }
        return flagged, by_id

    def test_naive_rule_is_not_near_perfect(self, inflated):
        accounts, payments, _ = inflated
        flagged, by_id = self._naive_flags(accounts, payments, tenure_days=90, inbound_threshold=45)
        assert flagged, "The naive rule flagged nothing - thresholds need revisiting"

        true_positives = sum(1 for aid in flagged if by_id[aid].is_mule)
        precision = true_positives / len(flagged)

        assert precision < 0.85, (
            f"Naive rule reached precision {precision:.3f}. No institution gets that from "
            "two signals against a real adversary. The legitimate lookalike population is "
            "not biting, so every downstream metric would be an artefact of the generator."
        )

    def test_false_positives_land_on_the_lookalike_segments(self, inflated):
        """The false positives must be the population we expect to be hurt.

        If a velocity rule's errors fell randomly across the book, there would
        be no fairness story to tell. They do not: they land on newly opened
        businesses and on people collecting money for a group.
        """
        accounts, payments, _ = inflated
        flagged, by_id = self._naive_flags(accounts, payments, tenure_days=90, inbound_threshold=45)

        false_positives = [by_id[aid] for aid in flagged if not by_id[aid].is_mule]
        assert false_positives, "No false positives at all - see the precision test"

        segments = {a.segment for a in false_positives}
        assert segments & {"small_business", "community_collector"}, (
            f"False positives landed on {segments}, not on the newly-opened busy accounts "
            "the rule is expected to punish. The confound population is misconfigured."
        )

    def test_the_evasion_cohort_defeats_the_naive_rule(self, inflated):
        accounts, payments, _ = inflated
        flagged, _ = self._naive_flags(accounts, payments, tenure_days=90, inbound_threshold=45)

        evasive = [a for a in accounts if a.account_type is AccountType.MULE_EVASIVE]
        caught = sum(1 for a in evasive if a.account_id in flagged)
        missed_share = 1 - (caught / len(evasive)) if evasive else 0.0

        assert missed_share > 0.8, (
            f"The naive rule caught {caught} of {len(evasive)} evasive mules. The cohort "
            "exists to defeat exactly this rule by aging accounts past the tenure "
            "threshold; if it does not, it is not evading anything."
        )

    def test_the_naive_rule_breaches_the_intervention_guardrail(self, inflated):
        """Specification Section 9 caps the intervention rate at 0.5%.

        A rule that flags several percent of the book is not a detector, it is
        a queue nobody can work. Asserting the naive rule breaches the
        guardrail confirms the testbed has a realistic cost structure - the
        constraint that actually binds in production is review capacity, not
        accuracy.
        """
        accounts, payments, _ = inflated
        flagged, _ = self._naive_flags(accounts, payments, tenure_days=90, inbound_threshold=45)

        intervention_rate = len(flagged) / len(accounts)
        assert intervention_rate > 0.005, (
            f"Naive rule flags {intervention_rate:.2%} of accounts, inside the 0.5% "
            "guardrail. That would mean a two-signal rule is deployable as-is, which "
            "would make the network pointless."
        )


class TestArmsDeclareTheirOwnLimits:
    """The arms are not interchangeable and each says so.

    The mule population is derived from fraud volume, so the realistic arm
    holds far fewer mules. Any per-account statistic from it rests on a sample
    too small to bound, and a report generated from it should carry that
    caveat automatically rather than relying on whoever reads it to remember.
    """

    def test_every_arm_states_what_it_supports(self):
        for arm in (REALISTIC_ARM, INFLATED_ARM):
            assert arm.supports, f"{arm.name} arm does not declare its limits"

    def test_realistic_arm_holds_too_few_mules_for_account_analysis(self, realistic):
        accounts, _, _ = realistic
        mules = sum(1 for a in accounts if a.is_mule)
        assert mules < 60, (
            f"The realistic arm holds {mules} mules. If that has grown, the note in "
            "ArmSpec.supports saying it cannot bound per-account statistics needs "
            "revisiting."
        )

    def test_inflated_arm_holds_enough_mules_for_cohort_analysis(self, inflated):
        accounts, _, _ = inflated
        evasive = sum(1 for a in accounts if a.account_type is AccountType.MULE_EVASIVE)
        assert evasive >= 25, (
            f"Only {evasive} evasive mules in the inflated arm, which is the arm that is "
            "supposed to support cohort analysis."
        )

    def test_fraud_per_mule_resembles_a_collection_account(self, realistic, inflated):
        """A mule receiving four payments is a rounding error, not a mule."""
        for bundle, arm in ((realistic, REALISTIC_ARM), (inflated, INFLATED_ARM)):
            accounts, payments, _ = bundle
            mules = sum(1 for a in accounts if a.is_mule)
            fraud = sum(1 for p in payments if p.is_fraud)
            per_mule = fraud / mules
            assert 8 <= per_mule <= 60, (
                f"{arm.name} arm: {per_mule:.1f} fraudulent payments per mule. Outside the "
                "range where a mule looks like a collection point."
            )


class TestNoAccountIsATemplate:
    """Within-type heterogeneity.

    An earlier version gave every account of a type identical behavioural
    constants, so every classic mule swept within an eighteen-minute band and
    a single threshold separated them from businesses perfectly. These tests
    assert the population has genuine spread, because homogeneity is the
    failure mode that makes a benchmark look impressive and mean nothing.
    """

    def test_accounts_of_one_type_differ_from_each_other(self, inflated):
        accounts, _, _ = inflated
        for account_type in (
            AccountType.ESTABLISHED_PERSONAL,
            AccountType.SMALL_BUSINESS,
            AccountType.MULE_CLASSIC,
        ):
            rates = [
                a.behaviour.outbound_per_month for a in accounts if a.account_type is account_type
            ]
            assert len(rates) > 20
            spread = max(rates) / min(rates)
            assert spread > 3.0, (
                f"{account_type.value} outbound rates span only {spread:.1f}x. The "
                "archetype is acting as a template rather than a centre of mass, so a "
                "detector can memorise the parameter instead of learning the behaviour."
            )

    def test_no_single_sweep_delay_threshold_separates_the_classes(self, inflated):
        """Measure separability directly rather than proxy it.

        Sweep delay was the strongest free signal in an earlier version: the
        fastest legitimate sweeper held funds for eight hours, so any mule
        moving quicker was uniquely identifiable and the best single threshold
        scored an F1 of 0.92.

        Businesses on an automatic sweep to a concentration account are why
        that threshold does not work in production - they move funds within
        minutes, on a timer, entirely legitimately. This test searches every
        threshold and asserts the best one is not good enough to deploy.
        """
        import numpy as np

        accounts, _, _ = inflated
        mule_delays = sorted(a.behaviour.sweep_delay_hours for a in accounts if a.is_mule)
        legit_delays = sorted(
            a.behaviour.sweep_delay_hours
            for a in accounts
            if a.behaviour.sweeps_inbound and not a.is_mule
        )
        assert mule_delays and legit_delays

        best_f1 = 0.0
        for threshold in np.percentile(mule_delays + legit_delays, np.arange(1, 100)):
            true_positives = sum(1 for d in mule_delays if d <= threshold)
            false_positives = sum(1 for d in legit_delays if d <= threshold)
            false_negatives = len(mule_delays) - true_positives
            if true_positives:
                f1 = 2 * true_positives / (2 * true_positives + false_positives + false_negatives)
                best_f1 = max(best_f1, f1)

        assert best_f1 < 0.8, (
            f"The best single threshold on sweep delay reaches F1 {best_f1:.3f}. One "
            "feature should not come close to solving the problem - if it does, the "
            "legitimate fast-sweeping population is missing."
        )

    def test_mule_sophistication_is_continuous_not_bimodal(self, inflated):
        """No gap in the middle of the distribution.

        Two discrete archetypes teach a detector two discrete patterns. The
        classic and evasive labels are derived from this score after the fact
        and there is deliberately nothing behind them in the data.
        """
        accounts, _, _ = inflated
        scores = sorted(a.behaviour.sophistication for a in accounts if a.is_mule)
        assert len(scores) > 50

        middle = [s for s in scores if 0.35 <= s <= 0.65]
        assert len(middle) / len(scores) > 0.2, (
            "The middle of the sophistication range is sparse, which means the cohorts "
            "are effectively discrete again and a boundary exists to be found."
        )

    def test_mule_tenure_spans_orders_of_magnitude(self, inflated):
        accounts, _, _ = inflated
        ages = sorted((WINDOW_START - a.opened_at).days for a in accounts if a.is_mule)
        assert ages[0] < 30, "No mule is using a fresh account"
        assert ages[-1] > 300, "No mule is using an aged account"


class TestTimeLooksLikeTime:
    """Payments follow a weekly and daily rhythm.

    Uniform timestamps - which an earlier version produced - make every
    time-derived feature useless and let a detector ignore a real signal. They
    also make the velocity dimension easier than it should be, because a burst
    stands out against a flat background in a way it does not against a
    payday.
    """

    def test_weekends_are_quieter_than_weekdays(self, inflated):
        _, payments, _ = inflated
        from collections import Counter

        by_weekday = Counter(p.timestamp.weekday() for p in payments)
        weekday = sum(by_weekday[d] for d in range(5)) / 5
        weekend = sum(by_weekday[d] for d in (5, 6)) / 2
        assert weekday / weekend > 1.2, (
            f"Weekday to weekend ratio is {weekday / weekend:.2f}. Payment volume is "
            "effectively flat across the week."
        )

    def test_the_day_has_a_shape(self, inflated):
        _, payments, _ = inflated
        from collections import Counter

        by_hour = Counter(p.timestamp.hour for p in payments)
        counts = [by_hour[h] for h in range(24)]
        assert max(counts) / min(counts) > 2.0, (
            f"Peak to trough hour ratio is {max(counts) / min(counts):.2f}. Payments at "
            "04:00 are as likely as at noon, which no real rail has ever seen."
        )

    def test_accounts_keep_their_own_hours(self, inflated):
        accounts, _, _ = inflated
        peaks = {a.behaviour.peak_hour for a in accounts}
        assert len(peaks) > 8, (
            "Accounts share too few peak hours. A night-shift worker and a retiree "
            "should not transact on the same schedule."
        )
