"""Calibration constants for the synthetic testbed, with their sources.

Every number in this file is one of three things, and each is labelled:

  [PUBLISHED]  A figure from a named source, cited inline.
  [DERIVED]    Arithmetic on published figures, with the arithmetic shown.
  [ASSUMPTION] A value nobody publishes. Stated as an assumption so a reader
               knows it is ours, not evidence.

The third category matters more than it looks. Mule accounts as a percentage
of all accounts at an institution is not published by any bank, vendor or
regulator - BioCatch's "150,000 APAC mule accounts shut down in 2023" has no
denominator. Anyone who quotes a mule prevalence rate is quoting an
assumption, and this file says so rather than laundering one into a citation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

# ---------------------------------------------------------------------------
# Prevalence
# ---------------------------------------------------------------------------

REALISTIC_FRAUD_RATE = 0.0006
"""[PUBLISHED] Fraction of payments that are APP fraud.

UK Payment Systems Regulator, August 2024: 252,626 APP scam cases across
roughly 4.5 billion Faster Payments transactions in 2023, which is about 56
cases per million, or 0.0056%. Rounded up to 0.06% here - six times the UK
rate - because US instant rails are younger, less defended, and the closest
US proxy (Zelle's self-reported "0.02% of transactions result in a fraud or
scam report") measures reports received rather than fraud committed.

Using the UK figure directly would be more defensible as a citation but less
honest as a model of the US, where the reporting denominator is worse.
"""

INFLATED_FRAUD_RATE = 0.02
"""[ASSUMPTION] Prevalence for the inflated benchmark arm.

Roughly 33 times REALISTIC_FRAUD_RATE. It exists only so the benchmark set
holds enough positives to compute a stable confidence interval. Any metric
from this arm describes a world that does not exist and is reported beside
the realistic arm, never alone.
"""

FRAUD_PAYMENTS_PER_MULE = 18
"""[ASSUMPTION] How many victim payments one mule account absorbs before it
is abandoned.

The mule population is derived from this rather than set as a fraction of all
accounts, because that is the causal direction: an operator opens as many
collection accounts as the fraud volume requires. Sizing it the other way
round - a fixed percentage of the book - produced mules receiving four
fraudulent payments each in the realistic arm, which is not a collection
account, it is a rounding error.

Nobody publishes a figure for this. Mule accounts are burned quickly once
flagged, and the number is set here so that a mule looks like a collection
point rather than an incidental recipient.
"""

MIN_MULES_PER_ARM = 12
"""Floor, so the realistic arm still contains a mule population at all."""

EVASIVE_MULE_SHARE = 0.4
"""[ASSUMPTION] Share of mules reported as the evasion cohort.

A reporting band over a continuous sophistication score, not a distinct
population. The underlying behaviour has no boundary at this point or any
other - the label exists so results can be broken out, and the share is set
where the cohort is large enough to report on.

Operators adapt: they age accounts past tenure rules and pace inbound volume
beneath velocity rules. The share is invented, but its presence is not
optional - a testbed without an evasion cohort measures a detector against an
adversary that does not fight back, and reports a number that will not
survive contact with one.
"""


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------


class ScamProfile(StrEnum):
    """Scam categories, following the UK Finance and FTC breakdown.

    Using their taxonomy rather than one we invented means benchmark output
    can be read against published loss statistics instead of only against
    itself.
    """

    PURCHASE = "purchase"
    IMPERSONATION = "impersonation"
    ROMANCE = "romance"
    INVESTMENT = "investment"


@dataclass(frozen=True)
class AmountProfile:
    """A log-normal amount distribution, parameterised by its median."""

    median_cents: int
    sigma: float
    """Log-scale spread. Higher means a fatter tail."""


SCAM_AMOUNT_PROFILES: dict[ScamProfile, AmountProfile] = {
    # [PUBLISHED] UK Finance 2025: purchase scams are 71% of APP cases but a
    # much smaller share of losses, so high volume and low value.
    ScamProfile.PURCHASE: AmountProfile(median_cents=45_000, sigma=0.9),
    # [ASSUMPTION] Between purchase and romance. Impersonation losses fell in
    # both UK and US reporting through 2025; no median is published.
    ScamProfile.IMPERSONATION: AmountProfile(median_cents=180_000, sigma=1.1),
    # [ASSUMPTION] Romance losses run well above purchase scams in every
    # published breakdown, but no median is published for the US.
    ScamProfile.ROMANCE: AmountProfile(median_cents=300_000, sigma=1.2),
    # [PUBLISHED] FTC, 2025: median investment scam loss of $10,000, up from
    # $9,300 in 2024. The only scam category with a published median.
    ScamProfile.INVESTMENT: AmountProfile(median_cents=1_000_000, sigma=1.3),
}

SCAM_CATEGORY_WEIGHTS: dict[ScamProfile, float] = {
    # [PUBLISHED] UK Finance 2025: purchase scams are 71% of APP fraud cases.
    ScamProfile.PURCHASE: 0.71,
    ScamProfile.IMPERSONATION: 0.15,
    ScamProfile.ROMANCE: 0.08,
    # [PUBLISHED] UK Finance 2025: 14,893 investment cases out of 248,070
    # total, which is 6%.
    ScamProfile.INVESTMENT: 0.06,
}

TARGET_MEAN_FRAUD_RANGE_CENTS = (110_000, 320_000)
"""[DERIVED] Range the generated mean fraudulent transfer must fall within.

A range rather than a point, because the two available anchors describe
different populations and an honest calibration cannot claim more precision
than that:

  Lower bound, $1,108. US Senate Permanent Subcommittee on Investigations:
  192,878 P2P fraud and scam cases totalling $213.8 million across four
  institutions. This is peer-to-peer app fraud only, which skews small.

  Upper bound, about $3,000. UK Finance 2025: £576.4 million of APP losses
  across 248,070 cases, or roughly £2,324 per case. This covers all APP
  fraud including investment scams, so it skews larger than P2P alone.

The category mixture in this module is built from UK Finance category weights
and the FTC investment median, so it models APP fraud broadly rather than P2P
specifically, and its mean should land in the upper half of this range.

A note on the arithmetic, because it is the mistake that produced the first
version of this constant: for a log-normal the mean is exp(mu + sigma^2 / 2),
not the median exp(mu). Setting a median of $10,000 with sigma 1.3 produces a
mean above $23,000. The profiles below are chosen with that in mind, and the
calibration test asserts the resulting mean rather than trusting the medians.
"""

LEGITIMATE_P2P_AMOUNT = AmountProfile(median_cents=8_500, sigma=1.1)
"""[ASSUMPTION] Everyday person-to-person payments - splitting a bill, rent
share, paying a friend back. No public median exists for US instant-rail P2P.
Set low relative to fraud on purpose: the amount gap is real and a detector
that exploits it is exploiting something true."""

LEGITIMATE_BUSINESS_AMOUNT = AmountProfile(median_cents=42_000, sigma=1.3)
"""[ASSUMPTION] Payments to a small business - an invoice, a deposit, a
service. Overlaps the purchase-scam range deliberately."""


# ---------------------------------------------------------------------------
# Account behaviour
# ---------------------------------------------------------------------------


class AccountType(StrEnum):
    """Behavioural archetypes.

    The three confound types exist because a testbed made only of obvious
    mules and obvious civilians measures nothing. A new small business
    receiving payments from many first-time payers produces the same inbound
    velocity signature as a mule. A community collector - someone gathering
    contributions for a group gift or a funeral fund - produces the same
    signature *and* sweeps the funds onward afterwards.

    These are also the population the fairness analysis in M7 is about. New
    accounts, thin-file customers and remittance corridors are where a
    velocity-based signal concentrates its false positives, so the confound
    population and the fairness population are the same people.
    """

    ESTABLISHED_PERSONAL = "established_personal"
    NEW_PERSONAL = "new_personal"

    SMALL_BUSINESS = "small_business"
    """Confound: many inbound payments from first-time payers."""

    REMITTANCE_SENDER = "remittance_sender"
    """Confound: regular outbound transfers to a small fixed set of payees,
    often at predictable intervals. Structurally similar to a layering
    pattern, behaviourally nothing like it."""

    COMMUNITY_COLLECTOR = "community_collector"
    """Confound: the hardest one. Receives many small first-time transfers in
    a short window, then moves the total onward. That is the mule signature,
    performed by someone organising a group gift."""

    MULE_CLASSIC = "mule_classic"
    MULE_EVASIVE = "mule_evasive"

    @property
    def is_mule(self) -> bool:
        return self in {AccountType.MULE_CLASSIC, AccountType.MULE_EVASIVE}

    @property
    def is_confound(self) -> bool:
        """Legitimate accounts that resemble mules on at least one dimension."""
        return self in {
            AccountType.SMALL_BUSINESS,
            AccountType.COMMUNITY_COLLECTOR,
            AccountType.NEW_PERSONAL,
            AccountType.REMITTANCE_SENDER,
        }


ACCOUNT_TYPE_MIX: dict[AccountType, float] = {
    AccountType.ESTABLISHED_PERSONAL: 0.72,
    AccountType.NEW_PERSONAL: 0.12,
    AccountType.SMALL_BUSINESS: 0.08,
    AccountType.REMITTANCE_SENDER: 0.05,
    AccountType.COMMUNITY_COLLECTOR: 0.03,
}
"""[ASSUMPTION] Population mix of legitimate accounts. Mules are added
separately at MULE_ACCOUNT_RATE rather than drawn from this mix."""


@dataclass(frozen=True)
class BehaviourProfile:
    """How an account type moves money."""

    outbound_per_month: float

    inbound_per_month: float
    """Relative propensity to *receive* payments.

    Used as a selection weight when choosing a payee, not as a count. An
    earlier version of this generator declared these figures and then drew
    receivers uniformly, so every account type received the same inbound
    volume and the velocity dimension carried no information at all. The
    weighting is what makes a small business look busy and a personal account
    look quiet.

    For mule profiles this is deliberately low: a mule's inbound spike comes
    from the fraud stream, not from legitimate traffic, and its legitimate
    traffic should look like whatever the recruited account holder normally
    did.
    """

    distinct_payees: int
    first_time_payee_rate: float
    """Share of outbound payments going to a payee never paid before."""

    sweeps_inbound: bool = False
    """Whether the account moves inbound funds onward promptly."""

    sweep_delay: timedelta = timedelta(days=3)
    sweep_share: float = 0.0
    """Fraction of an inbound payment moved onward."""


BEHAVIOUR: dict[AccountType, BehaviourProfile] = {
    AccountType.ESTABLISHED_PERSONAL: BehaviourProfile(
        outbound_per_month=8.0,
        inbound_per_month=3.0,
        distinct_payees=12,
        first_time_payee_rate=0.15,
    ),
    AccountType.NEW_PERSONAL: BehaviourProfile(
        outbound_per_month=4.0,
        inbound_per_month=2.0,
        distinct_payees=4,
        first_time_payee_rate=0.55,
    ),
    AccountType.SMALL_BUSINESS: BehaviourProfile(
        outbound_per_month=6.0,
        inbound_per_month=38.0,
        distinct_payees=30,
        first_time_payee_rate=0.20,
        sweeps_inbound=True,
        sweep_delay=timedelta(days=5),
        sweep_share=0.55,
    ),
    AccountType.REMITTANCE_SENDER: BehaviourProfile(
        outbound_per_month=4.0,
        inbound_per_month=2.0,
        distinct_payees=3,
        first_time_payee_rate=0.05,
    ),
    AccountType.COMMUNITY_COLLECTOR: BehaviourProfile(
        outbound_per_month=3.0,
        inbound_per_month=22.0,
        distinct_payees=6,
        first_time_payee_rate=0.75,
        sweeps_inbound=True,
        sweep_delay=timedelta(days=2),
        sweep_share=0.85,
    ),
    AccountType.MULE_CLASSIC: BehaviourProfile(
        outbound_per_month=30.0,
        # Ordinary legitimate inbound. The velocity spike comes from the fraud
        # stream; giving mules a high legitimate weight too would let a
        # detector find them without the fraud ever being generated.
        inbound_per_month=3.0,
        distinct_payees=3,
        first_time_payee_rate=0.9,
        sweeps_inbound=True,
        sweep_delay=timedelta(minutes=9),
        sweep_share=0.95,
    ),
    AccountType.MULE_EVASIVE: BehaviourProfile(
        # Paced beneath a velocity threshold and swept after a delay long
        # enough to look like ordinary cash management.
        outbound_per_month=9.0,
        inbound_per_month=3.0,
        distinct_payees=5,
        first_time_payee_rate=0.6,
        sweeps_inbound=True,
        sweep_delay=timedelta(hours=26),
        sweep_share=0.7,
    ),
}


# ---------------------------------------------------------------------------
# Account age
# ---------------------------------------------------------------------------

NEWLY_OPENED_SHARE_OF_BUSY_ACCOUNTS = 0.3
"""[ASSUMPTION] Share of high-inbound legitimate accounts that are recently
opened.

This constant is the difference between a benchmark and a toy. Without it,
every high-velocity account in the population is also an old account, so
"young AND busy" identifies mules perfectly and a two-signal rule scores 100%
precision - a number no institution has ever achieved against real adversaries.

Newly opened businesses receiving their first payments, and people who open
an account to collect for a fundraiser, are the exact population a
tenure-plus-velocity rule punishes in production. They are also, not
coincidentally, the population the fairness analysis in M7 is about.
"""

ESTABLISHED_AGE_DAYS = (400, 4000)
NEW_ACCOUNT_AGE_DAYS = (3, 90)
CLASSIC_MULE_AGE_DAYS = (2, 45)
EVASIVE_MULE_AGE_DAYS = (200, 900)
"""[ASSUMPTION] Account age ranges in days at the start of the window.

The evasive range is the point of the cohort: an operator who buys or farms
aged accounts defeats a tenure rule entirely, and the only way to know how
much of a detector's performance rests on tenure is to include accounts where
tenure says nothing.
"""


# ---------------------------------------------------------------------------
# Institutions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstitutionSpec:
    institution_id: str
    display_name: str
    account_count: int
    is_large: bool = False


INSTITUTIONS: tuple[InstitutionSpec, ...] = (
    InstitutionSpec("metro-savings-bank", "Metro Savings Bank", 3000, is_large=True),
    InstitutionSpec("first-community-cu", "First Community Credit Union", 2000),
    InstitutionSpec("riverside-federal-cu", "Riverside Federal Credit Union", 1500),
    InstitutionSpec("summit-national-bank", "Summit National Bank", 1500),
)
"""Deliberately uneven.

[PUBLISHED] More than 95% of FedNow participants are community banks and
credit unions, and those are the institutions most likely to hold a mule
account and least likely to detect one. An even four-way split would hide
that asymmetry, and it would also let code accidentally depend on equal
institution sizes.
"""

MULE_CONCENTRATION_AT_SMALL_INSTITUTIONS = 1.8
"""[ASSUMPTION] Multiplier on mule rate at non-large institutions.

Expresses the exposure asymmetry above. Not a measured figure - it is the
modelling choice that makes the testbed capable of showing the problem the
specification describes.
"""


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmSpec:
    """One prevalence arm of the benchmark."""

    name: str
    fraud_rate: float
    window_days: int
    rationale: str
    supports: str = ""
    """What this arm can and cannot be used to claim.

    The arms are not interchangeable. Because the mule population is derived
    from fraud volume, the realistic arm holds far fewer mule accounts, and
    any per-account or per-cohort statistic from it rests on a sample too
    small to bound. Stating the boundary on the arm itself means a report
    generated from it carries the caveat automatically.
    """


REALISTIC_ARM = ArmSpec(
    name="realistic",
    fraud_rate=REALISTIC_FRAUD_RATE,
    window_days=360,
    rationale=(
        "At realistic prevalence a 90-day window over this population yields too few "
        "fraudulent payments to bound any metric usefully, so the arm runs for 360 days "
        "instead. The arms therefore differ in duration as well as rate, which is stated "
        "here rather than buried: comparing their absolute counts is meaningless, and only "
        "their rates are comparable."
    ),
    supports=(
        "Payment-level metrics only: interception rate by count and by value, and the "
        "intervention rate on all payments. It holds too few mule accounts to support "
        "any per-account precision figure or any statement about the evasion cohort."
    ),
)

INFLATED_ARM = ArmSpec(
    name="inflated",
    fraud_rate=INFLATED_FRAUD_RATE,
    window_days=90,
    rationale=(
        "Elevated prevalence over the specification's 90-day window. Produces enough "
        "positives for stable intervals, at a rate roughly 33 times what institutions "
        "actually see."
    ),
    supports=(
        "Per-account and per-cohort analysis, including the evasion cohort and the "
        "segment fairness breakdown. Its payment-level rates are not comparable to "
        "anything an institution observes, because its prevalence is invented."
    ),
)

ARMS: tuple[ArmSpec, ...] = (REALISTIC_ARM, INFLATED_ARM)

TRAIN_TEST_SPLIT_FRACTION = 0.667
"""Temporal split point. Earlier two thirds train, later third test.

Temporal rather than random, because mule accounts age and operator tactics
shift within a window. A random split lets a model learn from an account's
future, which is not a capability it will have in production.
"""


@dataclass(frozen=True)
class GeneratorSettings:
    """Everything needed to reproduce a testbed exactly."""

    seed: int = 20260917
    arm: ArmSpec = REALISTIC_ARM
    institutions: tuple[InstitutionSpec, ...] = INSTITUTIONS
    network_salt: str = "interlock-testbed-salt-not-for-production"
    account_type_mix: dict[AccountType, float] = field(
        default_factory=lambda: dict(ACCOUNT_TYPE_MIX)
    )

    @property
    def total_accounts(self) -> int:
        return sum(i.account_count for i in self.institutions)
