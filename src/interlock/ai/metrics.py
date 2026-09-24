"""How the AI is actually doing, measured from the audit trail.

The number that matters for an assistant is not how often it is right in a
benchmark but how often the people using it agree with it, and where they do
not. Every transition recorded while a recommendation was on screen carries
``ai_suggested`` on the chain, so both are counted from evidence rather than
from a separate log someone could forget to write.

An override is not a model error. A person who knows something the model was
not told should overrule it, and a high override rate on one action is a
signal to look at what the model is missing, not a score to push up.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from interlock.recall.ledger import LedgerEntry


@dataclass(frozen=True, slots=True)
class AdoptionReport:
    decisions_with_ai: int
    followed: int
    overridden: int
    decisions_without_ai: int
    by_suggestion: dict[str, tuple[int, int]] = field(default_factory=dict)
    """suggested action -> (followed, overridden)."""
    override_pairs: list[tuple[str, str, int]] = field(default_factory=list)
    """(suggested, chosen instead, count), most common first."""

    @property
    def follow_rate(self) -> float | None:
        return self.followed / self.decisions_with_ai if self.decisions_with_ai else None


def adoption(entries: list[LedgerEntry]) -> AdoptionReport:
    """Count followed and overridden AI suggestions on the chain."""
    followed = overridden = without = 0
    per: dict[str, list[int]] = {}
    pairs: Counter[tuple[str, str]] = Counter()

    for item in entries:
        payload = item.payload
        if "transition" not in payload:
            continue
        suggested = payload.get("ai_suggested")
        if not suggested:
            without += 1
            continue
        chosen = str(payload["transition"])
        bucket = per.setdefault(str(suggested), [0, 0])
        if chosen == suggested:
            followed += 1
            bucket[0] += 1
        else:
            overridden += 1
            bucket[1] += 1
            pairs[(str(suggested), chosen)] += 1

    return AdoptionReport(
        decisions_with_ai=followed + overridden,
        followed=followed,
        overridden=overridden,
        decisions_without_ai=without,
        by_suggestion={k: (v[0], v[1]) for k, v in sorted(per.items())},
        override_pairs=[(a, b, n) for (a, b), n in pairs.most_common(10)],
    )
