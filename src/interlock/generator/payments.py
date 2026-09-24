"""Generate the payment stream.

Two populations of payments share one ledger: ordinary traffic produced by
each account's behavioural profile, and fraudulent traffic where a victim is
manipulated into paying a mule. Both carry ground truth.

The onward sweep is generated for every account whose profile sweeps, not only
for mules. That is the point: a community collector gathering contributions
and a mule laundering proceeds produce the same shape, and a testbed where
only mules sweep would reward a detector for finding a pattern that does not
discriminate in the real world.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from interlock.generator.behaviour import (
    MULE_CAMPAIGN_DAYS,
    day_weights,
    interpolate_log,
    sample_timestamps,
)
from interlock.generator.config import (
    LEGITIMATE_BUSINESS_AMOUNT,
    LEGITIMATE_P2P_AMOUNT,
    SCAM_AMOUNT_PROFILES,
    SCAM_CATEGORY_WEIGHTS,
    AccountType,
    AmountProfile,
    ArmSpec,
    GeneratorSettings,
    ScamProfile,
)
from interlock.generator.population import Account


@dataclass(frozen=True, slots=True)
class Payment:
    """One payment on the ledger."""

    payment_id: str
    timestamp: datetime

    sender_account_id: str
    sender_institution_id: str
    receiver_account_id: str
    receiver_institution_id: str
    receiver_account_hash: str

    amount_cents: int
    rail: str
    is_first_time_payee: bool

    is_fraud: bool
    scam_category: str | None
    is_onward_sweep: bool
    """True if this payment moves funds that recently arrived. Present on
    legitimate collectors and businesses as well as mules."""

    @property
    def is_cross_institution(self) -> bool:
        return self.sender_institution_id != self.receiver_institution_id


def _draw_amount(rng: np.random.Generator, profile: AmountProfile) -> int:
    """Log-normal draw, in integer cents.

    Parameterised by median because a median is what published fraud
    statistics report. For a log-normal, the median is exp(mu), so mu is
    log(median) and no separate mean calibration is needed.
    """
    mu = np.log(profile.median_cents)
    value = rng.lognormal(mean=mu, sigma=profile.sigma)
    return max(100, int(value))


def _pick_scam_category(rng: np.random.Generator) -> ScamProfile:
    categories = list(SCAM_CATEGORY_WEIGHTS.keys())
    weights = np.array([SCAM_CATEGORY_WEIGHTS[c] for c in categories], dtype=float)
    weights /= weights.sum()
    return categories[int(rng.choice(len(categories), p=weights))]


def _pick_rail(rng: np.random.Generator) -> str:
    # [PUBLISHED] RTP processed more than $1.3 trillion in 2025 against
    # FedNow's $853.4 billion, and RTP's transaction volume is far higher
    # still - roughly 1.36 million per day versus FedNow's 30,000. Weighted
    # heavily toward RTP to reflect that.
    return "rtp" if rng.random() < 0.85 else "fednow"


class PaymentLedger:
    """Accumulates payments and tracks the state needed to generate them."""

    def __init__(self, rng: np.random.Generator) -> None:
        self._rng = rng
        self.payments: list[Payment] = []
        self._known_payees: dict[str, set[str]] = {}
        self._pending_sweeps: dict[str, list[tuple[datetime, int]]] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"pay_{self._counter:010d}"

    def record(
        self,
        *,
        timestamp: datetime,
        sender: Account,
        receiver: Account,
        amount_cents: int,
        is_fraud: bool = False,
        scam_category: str | None = None,
        is_onward_sweep: bool = False,
    ) -> Payment:
        seen = self._known_payees.setdefault(sender.account_id, set())
        first_time = receiver.account_id not in seen
        seen.add(receiver.account_id)

        payment = Payment(
            payment_id=self._next_id(),
            timestamp=timestamp,
            sender_account_id=sender.account_id,
            sender_institution_id=sender.institution_id,
            receiver_account_id=receiver.account_id,
            receiver_institution_id=receiver.institution_id,
            receiver_account_hash=receiver.account_hash,
            amount_cents=amount_cents,
            rail=_pick_rail(self._rng),
            is_first_time_payee=first_time,
            is_fraud=is_fraud,
            scam_category=scam_category,
            is_onward_sweep=is_onward_sweep,
        )
        self.payments.append(payment)

        behaviour = receiver.behaviour
        if behaviour.sweeps_inbound:
            swept = int(amount_cents * behaviour.sweep_share)
            if swept > 100:
                due = timestamp + timedelta(hours=behaviour.sweep_delay_hours)
                self._pending_sweeps.setdefault(receiver.account_id, []).append((due, swept))

        return payment

    def due_sweeps(self, account_id: str, now: datetime) -> list[int]:
        """Pop any sweeps that have come due for this account."""
        pending = self._pending_sweeps.get(account_id)
        if not pending:
            return []
        due = [amount for when, amount in pending if when <= now]
        self._pending_sweeps[account_id] = [(w, a) for w, a in pending if w > now]
        return due


def generate_payments(
    *,
    settings: GeneratorSettings,
    arm: ArmSpec,
    accounts: list[Account],
    window_start: datetime,
) -> list[Payment]:
    """Generate the full payment stream for one arm."""
    rng = np.random.default_rng(settings.seed + 1)
    ledger = PaymentLedger(rng)

    by_id = {a.account_id: a for a in accounts}
    non_mules = [a for a in accounts if not a.is_mule]  # victims and sweep destinations
    mules = [a for a in accounts if a.is_mule]

    # Receivers are drawn from the whole population, mules included. A mule is
    # a real person's account that was recruited or sold, so it carries
    # ordinary traffic too. Restricting legitimate payments to non-mules would
    # leave every mule with an inbound history that is 100% fraud, and would
    # give the evasive cohort - aged 200 to 900 days - no history at all,
    # which is itself a giveaway no real operator would leave.
    _generate_ordinary_traffic(
        rng=rng,
        ledger=ledger,
        accounts=accounts,
        by_id=by_id,
        candidates=accounts,
        arm=arm,
        window_start=window_start,
    )

    if mules:
        _generate_fraud_traffic(
            rng=rng,
            ledger=ledger,
            victims=non_mules,
            mules=mules,
            arm=arm,
            window_start=window_start,
        )

    ledger.payments.sort(key=lambda p: p.timestamp)
    return ledger.payments


def _generate_ordinary_traffic(
    *,
    rng: np.random.Generator,
    ledger: PaymentLedger,
    accounts: list[Account],
    by_id: dict[str, Account],
    candidates: list[Account],
    arm: ArmSpec,
    window_start: datetime,
) -> None:
    """Everyday payments, driven by each account's behavioural profile."""
    months = arm.window_days / 30.0

    # Receivers are drawn in proportion to their inbound propensity, so a
    # small business at 38/month attracts roughly twelve times the traffic of
    # a personal account at 3/month. Drawing uniformly instead - which an
    # earlier version did - gave every archetype the same inbound volume and
    # left the velocity dimension carrying no information whatsoever.
    inbound_weights = np.array([a.behaviour.inbound_weight for a in candidates], dtype=float)
    inbound_weights /= inbound_weights.sum()

    base_day_weights = day_weights(window_start, arm.window_days)

    for account in accounts:
        behaviour = account.behaviour

        # Mules' ordinary outbound is their sweep, generated below from the
        # fraud they receive. Giving them independent outbound traffic too
        # would double-count and make them trivially separable by volume.
        if account.account_type.is_mule:
            continue

        payee_pool = _payee_pool(
            rng, account, candidates, behaviour.distinct_payees, inbound_weights
        )
        if not payee_pool:
            continue

        n_payments = int(rng.poisson(behaviour.outbound_per_month * months))
        for timestamp in sample_timestamps(
            rng,
            count=n_payments,
            window_start=window_start,
            window_days=arm.window_days,
            behaviour=behaviour,
            base_day_weights=base_day_weights,
        ):
            if rng.random() < behaviour.first_time_payee_rate:
                receiver = candidates[int(rng.integers(len(candidates)))]
            else:
                receiver = payee_pool[int(rng.integers(len(payee_pool)))]
            if receiver.account_id == account.account_id:
                continue

            amount_profile = (
                LEGITIMATE_BUSINESS_AMOUNT
                if receiver.account_type is AccountType.SMALL_BUSINESS
                else LEGITIMATE_P2P_AMOUNT
            )
            ledger.record(
                timestamp=timestamp,
                sender=account,
                receiver=receiver,
                amount_cents=_draw_amount(rng, amount_profile),
            )

    _emit_sweeps(rng=rng, ledger=ledger, accounts=accounts, by_id=by_id, candidates=candidates)


def _payee_pool(
    rng: np.random.Generator,
    account: Account,
    candidates: list[Account],
    size: int,
    weights: np.ndarray | None = None,
) -> list[Account]:
    """The regular payees an account pays more than once.

    Weighted the same way as one-off payments: the people and businesses you
    pay repeatedly come from the same population as the ones you pay once, so
    a busy merchant appears in many accounts' pools.
    """
    if not candidates:
        return []
    picks = rng.choice(len(candidates), size=min(size, len(candidates)), replace=False, p=weights)
    return [
        candidates[int(i)] for i in picks if candidates[int(i)].account_id != account.account_id
    ]


def _generate_fraud_traffic(
    *,
    rng: np.random.Generator,
    ledger: PaymentLedger,
    victims: list[Account],
    mules: list[Account],
    arm: ArmSpec,
    window_start: datetime,
) -> None:
    """Victim-to-mule payments.

    Fraud volume is set as a share of total payment volume so the arm's
    prevalence means what it says: fraudulent payments divided by all
    payments, which is how every published rate is expressed.
    """
    ordinary_count = len(ledger.payments)
    target_fraud = max(1, round(ordinary_count * arm.fraud_rate / (1 - arm.fraud_rate)))
    window_seconds = arm.window_days * 86_400

    # Mule campaigns are bursty: a destination account is circulated among
    # several victims over a short period, then abandoned. Generating fraud as
    # independent uniform events would erase the inbound-velocity signature
    # the network is meant to detect.
    campaigns = max(1, len(mules))
    per_mule = np.maximum(1, rng.poisson(target_fraud / campaigns, size=campaigns))

    emitted = 0
    for mule_index, mule in enumerate(mules):
        if emitted >= target_fraud:
            break

        campaign_start = window_start + timedelta(seconds=int(rng.random() * window_seconds * 0.9))
        # Campaign length rides the operator's own sophistication rather than a
        # two-valued constant, so there is no duration that marks a cohort.
        burst_days = interpolate_log(*MULE_CAMPAIGN_DAYS, mule.behaviour.sophistication) * float(
            rng.lognormal(0.0, 0.25)
        )

        for _ in range(int(per_mule[mule_index])):
            if emitted >= target_fraud:
                break
            victim = victims[int(rng.integers(len(victims)))]
            offset = timedelta(seconds=int(rng.random() * burst_days * 86_400))
            timestamp = campaign_start + offset
            if timestamp >= window_start + timedelta(days=arm.window_days):
                continue

            category = _pick_scam_category(rng)
            ledger.record(
                timestamp=timestamp,
                sender=victim,
                receiver=mule,
                amount_cents=_draw_amount(rng, SCAM_AMOUNT_PROFILES[category]),
                is_fraud=True,
                scam_category=category.value,
            )
            emitted += 1

    _emit_sweeps(
        rng=rng,
        ledger=ledger,
        accounts=mules,
        by_id={a.account_id: a for a in mules},
        candidates=mules,
        onward_only=True,
    )


def _emit_sweeps(
    *,
    rng: np.random.Generator,
    ledger: PaymentLedger,
    accounts: list[Account],
    by_id: dict[str, Account],
    candidates: list[Account],
    onward_only: bool = False,
) -> None:
    """Emit the onward movement of funds that arrived earlier.

    Runs for mules and legitimate sweepers alike. The destination pool differs
    - a mule moves funds to another mule or off-network, a business pays
    suppliers - but the shape on the ledger is deliberately similar.
    """
    if not candidates:
        return

    inbound_by_receiver: dict[str, list[tuple[datetime, int]]] = {}
    for payment in ledger.payments:
        if onward_only and not payment.is_fraud:
            continue
        inbound_by_receiver.setdefault(payment.receiver_account_id, []).append(
            (payment.timestamp, payment.amount_cents)
        )

    for account in accounts:
        behaviour = account.behaviour
        if not behaviour.sweeps_inbound:
            continue

        for arrived_at, amount in inbound_by_receiver.get(account.account_id, []):
            swept = int(amount * behaviour.sweep_share)
            if swept <= 100:
                continue
            delay = timedelta(hours=behaviour.sweep_delay_hours)
            jitter = timedelta(seconds=int(rng.random() * delay.total_seconds()))
            destination = candidates[int(rng.integers(len(candidates)))]
            if destination.account_id == account.account_id:
                continue
            ledger.record(
                timestamp=arrived_at + delay + jitter,
                sender=account,
                receiver=destination,
                amount_cents=swept,
                is_onward_sweep=True,
            )
