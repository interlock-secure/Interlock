"""Turning a case into features, with a hard boundary against leakage.

The leak this module exists to prevent
--------------------------------------
:class:`~interlock.generator.recall_cases.GeneratedCase` carries both the
inputs and the outcome, because it is a labelled training row. Some of its
fields are only knowable *after* the case is worked, and some are the
generator's own latent parameters, which no production system has. Either kind
in the feature set produces a model that scores beautifully and cannot be used.

How the boundary is enforced
----------------------------
:func:`feature_matrix` never hands ``features_for`` the case itself. It hands
over a :class:`RankingTimeView`, which forwards every attribute except those in
:data:`FORBIDDEN_FEATURES` and raises :class:`LeakageError` on those. That
catches a direct read, ``getattr``, and a read laundered through a helper,
because the helper receives the view too.

An earlier version searched ``features_for``'s source text for ``.field`` and
missed all three of those. It is gone. What the view does not stop is someone
deliberately unwrapping it; that is evasion rather than a mistake, and a guard
against mistakes is what this is.

Counterparty history, as of each row
------------------------------------
The counterparty features are estimated from cases that had *closed before the
row arrived* - closure being the counterparty's response - which is the only
information a live system would have. The first fix built one table from every
fitting row, so each fitting row's feature included its own label, and a
counterparty seen three times was scored on an estimate that already knew the
answer. :func:`history_as_of` does a single sweep in time order instead.

What the fix cost in AUC is in ``docs/BUILD_LOG.md``, measured rather than
remembered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from interlock.generator.recall_cases import GeneratedCase
from interlock.schema.common import Rail

POST_OUTCOME_FIELDS: frozenset[str] = frozenset(
    {
        "responded_after_hours",
        "answered_within_window",
        "share_remaining",
        "recovered",
        "recovered_cents",
    }
)
"""Fields that exist only after the outcome."""

GENERATOR_LATENT_FIELDS: frozenset[str] = frozenset(
    {
        "counterparty_median_response_hours",
        "counterparty_return_willingness",
        "counterparty_answers_within_window",
        "scam_category",
        "split",
    }
)
"""The generator's own parameters and bookkeeping.

The true counterparty parameters are what the label was drawn from; no bank
knows them. ``scam_category`` is the generator's ground truth, not what an
operator sees at intake (that is ``reason``). ``split`` is evaluation
bookkeeping."""

FORBIDDEN_FEATURES: frozenset[str] = POST_OUTCOME_FIELDS | GENERATOR_LATENT_FIELDS
"""Never readable while building features."""

FEATURE_NAMES: tuple[str, ...] = (
    "log_minutes_since_settlement",
    "log_amount_cents",
    "hour_of_day",
    "day_of_week",
    "is_weekend",
    "counterparty_mean_log_response_hours",
    "counterparty_recovery_rate_estimate",
    "counterparty_history_is_established",
    "arrived_structured",
    "rail_is_instant",
    "rail_fednow",
    "rail_rtp",
    "rail_ach",
    "notice_lag_hours_log",
    "raise_lag_hours_log",
)
"""The feature set, in the order :func:`feature_matrix` emits.

Two design notes worth stating:

**Logs, not raw values.** Elapsed minutes span four orders of magnitude and
amounts three. Gradient boosting is invariant to monotone transforms so this
changes nothing for the tree model, but it makes the logistic-regression
baseline a fair comparison rather than a straw man.

**The counterparty features are estimates, and their names say so.** They were
once called ``counterparty_return_willingness`` and
``counterparty_answers_within_window``, which were the generator's parameter
names - and reading like the parameter is how they came to *be* the parameter.

**The two lags are separated.** Time from settlement to the victim noticing,
and from the victim reporting to the request arriving, have different causes:
the first is about the scam type, the second about the sending institution's
own process. A model given only their sum cannot tell a fast institution
handling a slow-to-surface scam from a slow one handling an obvious scam.
"""


POPULATION_PRIOR_RECOVERY = 0.10
"""Fallback for a counterparty with no closed history yet.

Deliberately not zero and not the true rate. A counterparty nobody has dealt
with is genuinely unknown, and the model should treat it as unremarkable
rather than as certainly bad.
"""

MINIMUM_HISTORY = 3
"""Closed cases needed before a counterparty's own rate is used.

Below this the estimate is one or two coin flips. Shrinking toward the
population prior is the standard fix and it matters here because most
counterparties in a real book are tail institutions with almost no history.
"""


class LeakageError(RuntimeError):
    """A forbidden field was read while building features."""


class RankingTimeView:
    """A case as it looks when it arrives, before anyone has worked it.

    Forwards every attribute except :data:`FORBIDDEN_FEATURES`, which raise.
    """

    __slots__ = ("_RankingTimeView__case",)

    def __init__(self, case: GeneratedCase) -> None:
        object.__setattr__(self, "_RankingTimeView__case", case)

    def __getattr__(self, name: str):
        if name in FORBIDDEN_FEATURES:
            kind = (
                "is only knowable after the case is worked"
                if name in POST_OUTCOME_FIELDS
                else "is the generator's own parameter, which no bank has"
            )
            raise LeakageError(
                f"feature code read case.{name}, which {kind}. A model given it scores "
                f"beautifully and cannot be used at ranking time."
            )
        return getattr(object.__getattribute__(self, "_RankingTimeView__case"), name)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a ranking-time view is read-only")

    def _no_escape(self, *args: object, **kwargs: object):
        # __getattr__ only covers names the class lacks, and object supplies
        # these; a third review got the whole case back through __replace__
        # and the pickling hooks.
        raise LeakageError("a ranking-time view cannot be copied, pickled or unwrapped")

    __replace__ = _no_escape
    __getstate__ = _no_escape
    __reduce__ = _no_escape
    __reduce_ex__ = _no_escape
    __copy__ = _no_escape
    __deepcopy__ = _no_escape


def as_ranking_time(case: GeneratedCase | RankingTimeView) -> RankingTimeView:
    return case if isinstance(case, RankingTimeView) else RankingTimeView(case)


@dataclass(frozen=True, slots=True)
class CounterpartyHistory:
    """What is known about a counterparty from cases already closed.

    An *estimate*, not the generator's parameter. It is noisy, it is absent
    for a counterparty nobody has dealt with, and a production deployment
    would have exactly the same two problems.
    """

    recovery_rate: float
    mean_log_response_hours: float
    """Mean of ``log1p(hours)``. A mean rather than a median so it can be
    maintained incrementally in :func:`history_as_of`; on the log scale the
    two differ little for a skewed delay."""
    closed_cases: int

    @property
    def is_established(self) -> bool:
        return self.closed_cases >= MINIMUM_HISTORY


def _estimate(n: int, recovered: int, sum_log_hours: float) -> CounterpartyHistory:
    # Shrink toward the population prior. With three closed cases an observed
    # rate of 1.0 is not evidence of anything.
    weight = n / (n + MINIMUM_HISTORY)
    rate = weight * (recovered / n) + (1 - weight) * POPULATION_PRIOR_RECOVERY
    return CounterpartyHistory(
        recovery_rate=float(rate),
        mean_log_response_hours=sum_log_hours / n,
        closed_cases=n,
    )


def closed_at(case: GeneratedCase) -> datetime:
    """When a case's outcome became known: the counterparty's response."""
    return case.received_at + timedelta(hours=max(case.responded_after_hours, 0.0))


def counterparty_history(closed: list[GeneratedCase]) -> dict[str, CounterpartyHistory]:
    """Per-counterparty estimates from every case given, regardless of time.

    For reporting and for ranking a fresh queue against everything closed so
    far. Not for building training rows - use :func:`history_as_of`, or each
    row's feature includes its own label.
    """
    totals: dict[str, list[float]] = {}
    for case in closed:
        n, rec, logs = totals.get(case.requesting_institution_id, [0, 0, 0.0])
        totals[case.requesting_institution_id] = [
            n + 1,
            rec + int(case.recovered),
            logs + _safe_log(case.responded_after_hours),
        ]
    return {k: _estimate(int(n), int(r), s) for k, (n, r, s) in totals.items()}


def history_as_of(
    rows: list[GeneratedCase], closed: list[GeneratedCase]
) -> list[CounterpartyHistory | None]:
    """For each row, its counterparty's estimate from cases closed before it arrived.

    One sweep: closures and arrivals merged in time order, running counts per
    counterparty. A closure at exactly the arrival instant is not counted -
    the row could not have seen it. A row never counts itself, because a case
    closes strictly after it arrives or, with a zero delay, at that instant.
    """
    closures = sorted(closed, key=closed_at)
    order = sorted(range(len(rows)), key=lambda i: rows[i].received_at)

    running: dict[str, list[float]] = {}
    result: list[CounterpartyHistory | None] = [None] * len(rows)
    next_closure = 0

    for index in order:
        arrived = rows[index].received_at
        while next_closure < len(closures) and closed_at(closures[next_closure]) < arrived:
            done = closures[next_closure]
            tally = running.setdefault(done.requesting_institution_id, [0, 0, 0.0])
            tally[0] += 1
            tally[1] += int(done.recovered)
            tally[2] += _safe_log(done.responded_after_hours)
            next_closure += 1

        tally = running.get(rows[index].requesting_institution_id)
        if tally:
            result[index] = _estimate(int(tally[0]), int(tally[1]), tally[2])

    return result


def _safe_log(value: float) -> float:
    return float(np.log1p(max(value, 0.0)))


def features_for(
    case: GeneratedCase | RankingTimeView, known: CounterpartyHistory | None = None
) -> list[float]:
    """One case to one feature row.

    Args:
        known: this counterparty's estimate as of the case's arrival, from
            :func:`history_as_of`. None means nothing had closed yet - the
            correct behaviour on a cold start and what the first weeks of a
            real deployment look like.
    """
    case = as_ranking_time(case)
    received = case.received_at
    notice_lag = (case.victim_reported_at - case.original_settled_at).total_seconds() / 3600.0
    raise_lag = (case.received_at - case.victim_reported_at).total_seconds() / 3600.0

    return [
        _safe_log(case.minutes_since_settlement),
        _safe_log(float(case.amount_cents)),
        float(received.hour),
        float(received.weekday()),
        1.0 if received.weekday() >= 5 else 0.0,
        known.mean_log_response_hours if known else 0.0,
        known.recovery_rate if known else POPULATION_PRIOR_RECOVERY,
        1.0 if (known and known.is_established) else 0.0,
        1.0 if case.arrived_structured else 0.0,
        1.0 if case.rail in {Rail.FEDNOW.value, Rail.RTP.value} else 0.0,
        1.0 if case.rail == Rail.FEDNOW.value else 0.0,
        1.0 if case.rail == Rail.RTP.value else 0.0,
        1.0 if case.rail == Rail.ACH.value else 0.0,
        _safe_log(notice_lag),
        _safe_log(raise_lag),
    ]


def feature_matrix(
    cases: list[GeneratedCase], closed: list[GeneratedCase] | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Features, labels and per-case value.

    Args:
        closed: cases whose outcomes may inform counterparty estimates. Each
            row only sees those that closed before it arrived.

    Returns:
        ``(X, y, value_cents)``. The third is not a feature - it is what a
        recovery on this case is worth, which the ranking policy needs and the
        model must never see.

    Raises:
        LeakageError: if feature code reads a forbidden field. Enforced by
            handing it a :class:`RankingTimeView`, never the case.
    """
    if not cases:
        empty = np.zeros((0, len(FEATURE_NAMES)))
        return empty, np.zeros(0, dtype=bool), np.zeros(0)

    known = history_as_of(cases, closed or [])
    x = np.array(
        [features_for(RankingTimeView(c), k) for c, k in zip(cases, known, strict=True)],
        dtype=float,
    )
    y = np.array([c.recovered for c in cases], dtype=bool)
    value = np.array([float(c.amount_cents) for c in cases], dtype=float)

    if x.shape[1] != len(FEATURE_NAMES):  # pragma: no cover - guarded by a test
        raise AssertionError(
            f"features_for produced {x.shape[1]} values but FEATURE_NAMES lists "
            f"{len(FEATURE_NAMES)}; the two must stay in step or every coefficient "
            f"is attributed to the wrong feature"
        )

    return x, y, value


def temporal_split(
    cases: list[GeneratedCase],
) -> tuple[list[GeneratedCase], list[GeneratedCase]]:
    """Split on the label the generator already assigned.

    Deliberately not recomputed here. The generator decided the boundary when
    it built the cases; recomputing it downstream is how two parts of a
    pipeline end up disagreeing about which rows are training data.
    """
    return (
        [c for c in cases if c.split == "train"],
        [c for c in cases if c.split == "test"],
    )
