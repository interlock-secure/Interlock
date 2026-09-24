"""Build the account population across four institutions.

Accounts carry a ground-truth label and an archetype. The label is what the
benchmark scores against; the archetype is what generated the behaviour, and
it is kept so that the fairness analysis in M7 can report false positives by
segment rather than only in aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from interlock.generator.behaviour import (
    MULE_TENURE_DAYS,
    AccountBehaviour,
    interpolate_log,
    sample_behaviour,
    sample_sophistication,
)
from interlock.generator.config import (
    ACCOUNT_TYPE_MIX,
    CLASSIC_MULE_AGE_DAYS,
    ESTABLISHED_AGE_DAYS,
    EVASIVE_MULE_AGE_DAYS,
    EVASIVE_MULE_SHARE,
    FRAUD_PAYMENTS_PER_MULE,
    MIN_MULES_PER_ARM,
    MULE_CONCENTRATION_AT_SMALL_INSTITUTIONS,
    NEW_ACCOUNT_AGE_DAYS,
    NEWLY_OPENED_SHARE_OF_BUSY_ACCOUNTS,
    AccountType,
    ArmSpec,
    GeneratorSettings,
    InstitutionSpec,
)
from interlock.schema.common import hash_account


@dataclass(frozen=True, slots=True)
class Account:
    """One account in the testbed.

    ``account_hash`` is what crosses the wire; ``account_id`` never leaves the
    generator. Keeping both makes the privacy boundary visible in the data
    model rather than only in the protocol.
    """

    account_id: str
    account_hash: str
    institution_id: str
    account_type: AccountType
    opened_at: datetime

    is_mule: bool
    """Ground truth. The benchmark scores against this and nothing else."""

    behaviour: AccountBehaviour
    """This account's own sampled parameters. Two accounts of the same
    archetype differ here, which is what stops an archetype being a template a
    detector can memorise."""

    def tenure_days(self, at: datetime) -> int:
        return max(0, (at - self.opened_at).days)

    @property
    def segment(self) -> str:
        """Fairness reporting segment.

        Deliberately coarser than account_type: an analyst investigating a
        false-positive gap wants to know "new accounts" or "collectors", not
        which generator archetype produced the row.
        """
        if self.account_type is AccountType.NEW_PERSONAL:
            return "thin_file_new"
        if self.account_type is AccountType.SMALL_BUSINESS:
            return "small_business"
        if self.account_type is AccountType.COMMUNITY_COLLECTOR:
            return "community_collector"
        if self.account_type is AccountType.REMITTANCE_SENDER:
            return "remittance_corridor"
        return "established_personal"


def _draw_age_days(rng: np.random.Generator, account_type: AccountType) -> int:
    """Account age at the start of the window.

    The branch that matters is the last one. Businesses and collectors are the
    high-inbound legitimate archetypes, and a share of them must be recently
    opened, or no legitimate account is ever both young and busy - which hands
    a tenure-plus-velocity rule perfect precision and makes every downstream
    number meaningless.
    """
    if account_type is AccountType.MULE_CLASSIC:
        low, high = CLASSIC_MULE_AGE_DAYS
    elif account_type is AccountType.MULE_EVASIVE:
        low, high = EVASIVE_MULE_AGE_DAYS
    elif account_type is AccountType.NEW_PERSONAL:
        low, high = NEW_ACCOUNT_AGE_DAYS
    elif account_type in (AccountType.SMALL_BUSINESS, AccountType.COMMUNITY_COLLECTOR):
        if rng.random() < NEWLY_OPENED_SHARE_OF_BUSY_ACCOUNTS:
            low, high = NEW_ACCOUNT_AGE_DAYS
        else:
            low, high = ESTABLISHED_AGE_DAYS
    else:
        low, high = ESTABLISHED_AGE_DAYS
    return int(rng.integers(low, high + 1))


def network_mule_target(settings: GeneratorSettings, arm: ArmSpec) -> int:
    """How many mule accounts the whole network needs for this arm.

    Derived from fraud volume, not from a share of the book. An operator opens
    as many collection accounts as the fraud they are running requires, so the
    causal chain is: payments in the window, times the arm's fraud rate, over
    the payments one mule absorbs before being burned.

    Sizing it the other way round - a fixed percentage of accounts - gives a
    mule four fraudulent payments in the realistic arm, which does not
    resemble a collection account at all.
    """
    # Approximate ordinary payment volume from the population's outbound
    # propensity. Only the order of magnitude matters here; the generator
    # reconciles the exact fraud count against the realised ledger later.
    from interlock.generator.config import ACCOUNT_TYPE_MIX, BEHAVIOUR

    mean_outbound = sum(
        BEHAVIOUR[t].outbound_per_month * share for t, share in ACCOUNT_TYPE_MIX.items()
    )
    months = arm.window_days / 30.0
    approx_payments = settings.total_accounts * mean_outbound * months
    approx_fraud = approx_payments * arm.fraud_rate

    return max(MIN_MULES_PER_ARM, round(approx_fraud / FRAUD_PAYMENTS_PER_MULE))


def _institution_mule_split(settings: GeneratorSettings, total_mules: int) -> dict[str, int]:
    """Spread the network's mules across institutions.

    Weighted by account count and then tilted toward the smaller
    institutions, expressing the exposure asymmetry in the specification: the
    participants least equipped to detect a mule are the most likely to hold
    one.
    """
    weights: dict[str, float] = {}
    for institution in settings.institutions:
        weight = float(institution.account_count)
        if not institution.is_large:
            weight *= MULE_CONCENTRATION_AT_SMALL_INSTITUTIONS
        weights[institution.institution_id] = weight

    total_weight = sum(weights.values())
    split = {k: int(total_mules * v / total_weight) for k, v in weights.items()}

    # Hand any rounding remainder to the smallest institution.
    remainder = total_mules - sum(split.values())
    if remainder:
        smallest = min(settings.institutions, key=lambda i: i.account_count).institution_id
        split[smallest] += remainder
    return split


def build_population(
    settings: GeneratorSettings,
    window_start: datetime,
    arm: ArmSpec | None = None,
) -> list[Account]:
    """Generate every account, labelled.

    Account ages are measured backwards from ``window_start`` so that tenure
    at the beginning of the observation window is a meaningful quantity.

    The mule population is sized from the arm's fraud volume, so the two arms
    hold different numbers of mules. That is deliberate and it is why the
    realistic arm cannot support per-account statistics - see ArmSpec.supports.
    """
    arm = arm or settings.arm
    rng = np.random.default_rng(settings.seed)
    accounts: list[Account] = []

    mule_split = _institution_mule_split(settings, network_mule_target(settings, arm))

    legit_types = list(ACCOUNT_TYPE_MIX.keys())
    legit_weights = np.array([ACCOUNT_TYPE_MIX[t] for t in legit_types], dtype=float)
    legit_weights /= legit_weights.sum()

    for institution in settings.institutions:
        n_mules = min(mule_split[institution.institution_id], institution.account_count)
        n_legit = institution.account_count - n_mules

        chosen_legit = rng.choice(len(legit_types), size=n_legit, p=legit_weights)

        for index in range(n_legit):
            account_type = legit_types[int(chosen_legit[index])]
            accounts.append(
                _make_account(
                    rng=rng,
                    settings=settings,
                    institution=institution,
                    index=index,
                    account_type=account_type,
                    window_start=window_start,
                )
            )

        for index in range(n_mules):
            # Sophistication is drawn first and the label follows from it. The
            # underlying behaviour is a continuum, so the classic/evasive split
            # is a reporting convenience with nothing behind it in the data -
            # there is no threshold a detector can find at the boundary.
            sophistication = sample_sophistication(rng)
            account_type = (
                AccountType.MULE_EVASIVE
                if sophistication >= (1 - EVASIVE_MULE_SHARE)
                else AccountType.MULE_CLASSIC
            )
            accounts.append(
                _make_account(
                    rng=rng,
                    settings=settings,
                    institution=institution,
                    index=n_legit + index,
                    account_type=account_type,
                    window_start=window_start,
                    sophistication=sophistication,
                )
            )

    return accounts


def _make_account(
    *,
    rng: np.random.Generator,
    settings: GeneratorSettings,
    institution: InstitutionSpec,
    index: int,
    account_type: AccountType,
    window_start: datetime,
    sophistication: float | None = None,
) -> Account:
    account_id = f"{institution.institution_id}:{index:06d}"

    if sophistication is not None:
        # A mule's tenure moves with its sophistication along a log scale, so
        # account age carries no clean boundary either.
        age_days = int(interpolate_log(*MULE_TENURE_DAYS, sophistication) * rng.lognormal(0.0, 0.3))
        age_days = max(1, age_days)
    else:
        age_days = _draw_age_days(rng, account_type)

    return Account(
        account_id=account_id,
        account_hash=hash_account(account_id, network_salt=settings.network_salt),
        institution_id=institution.institution_id,
        account_type=account_type,
        opened_at=window_start - timedelta(days=age_days),
        is_mule=account_type.is_mule,
        behaviour=sample_behaviour(rng, account_type, sophistication=sophistication),
    )


def summarise(accounts: list[Account]) -> dict[str, object]:
    """Population statistics for the run manifest."""
    by_type: dict[str, int] = {}
    by_institution: dict[str, int] = {}
    by_segment: dict[str, int] = {}
    mules = 0

    for account in accounts:
        by_type[account.account_type.value] = by_type.get(account.account_type.value, 0) + 1
        by_institution[account.institution_id] = by_institution.get(account.institution_id, 0) + 1
        by_segment[account.segment] = by_segment.get(account.segment, 0) + 1
        if account.is_mule:
            mules += 1

    return {
        "total_accounts": len(accounts),
        "mule_accounts": mules,
        "mule_rate": round(mules / len(accounts), 6) if accounts else 0.0,
        "by_account_type": dict(sorted(by_type.items())),
        "by_institution": dict(sorted(by_institution.items())),
        "by_fairness_segment": dict(sorted(by_segment.items())),
    }
