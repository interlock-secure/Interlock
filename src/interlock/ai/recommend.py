"""AI outcome recommendation: a suggestion with its reasons, never a decision.

For an open case the model is shown a fixed set of facts the system already
holds - rail, amount, reason, time since settlement, deadline, what has
happened so far - and asked which outcome fits and why. What comes back is
checked before anyone sees it:

- **The suggested action must be legal for this case right now.** The model
  cannot suggest reopening a closed case or a transition the state machine
  does not offer from the current state.
- **Acknowledging expiry is only suggestable once the deadline has passed.**
  Suggesting a case give up early is the most expensive thing the model could
  say, so it is structurally impossible rather than discouraged.
- **Every cited fact must be one the system supplied.** The rationale may only
  lean on facts by their key; a citation to anything else means the model is
  reasoning from something it made up, and the whole recommendation is
  rejected.

- **Every number in the rationale must be one it was given.**

What the checks cannot do is judge whether the reasoning is *good*: a
rationale that cites real facts and uses no invented numbers can still draw a
poor conclusion from them. That is why it is a suggestion shown beside the
facts, and why overrides are counted rather than discouraged.

A rejected or unavailable recommendation is shown as exactly that. The screen
never falls back to a default suggestion, because a suggestion nobody made is
worse than none.

The operator then records whatever outcome they choose. If a recommendation
was on screen, the transition carries it (``ai_suggested``) so the audit chain
shows whether the human followed the machine or overrode it - which is what
:mod:`interlock.ai.metrics` measures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from interlock.ai.client import AIUnavailableError, StructuredModel, default_model
from interlock.generator.recovery import expected_share_remaining
from interlock.recall.state import DISPOSITION_OF, CaseFile, Transition
from interlock.schema.common import utc_now

SYSTEM_PROMPT = """You advise a bank operations analyst working a payment recall case.

A recall is a request to return a payment. You receive facts about one case
as a list of key: value lines. Suggest the single most appropriate next
outcome from the allowed actions listed, and explain it in two or three plain
sentences an analyst can check quickly.

Rules:
- Use only the facts given. Cite them by key in cited_facts. Do not assume
  anything about account balances, the customer, or the counterparty beyond
  what is listed.
- If the facts are not enough to choose, pick the action that keeps the case
  moving (acknowledge or begin_investigation when allowed) and list what is
  missing in missing_information.
- Never suggest acknowledge_sla_expiry unless deadline_passed is true.
- Your suggestion is advice. A person decides."""

ActionValue = Literal[
    "acknowledge",
    "begin_investigation",
    "dispose_funds_returned",
    "dispose_partial_return",
    "dispose_funds_frozen",
    "dispose_insufficient_funds",
    "dispose_account_holder_disputes",
    "dispose_declined",
    "acknowledge_sla_expiry",
]


class ModelRecommendation(BaseModel):
    """The structured output the model is constrained to."""

    action: ActionValue
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=700)
    cited_facts: list[str] = Field(default_factory=list, max_length=8)
    missing_information: list[str] = Field(default_factory=list, max_length=5)


Status = Literal["ok", "rejected", "unavailable"]


@dataclass(frozen=True, slots=True)
class Recommendation:
    """What the console shows. ``status`` is never quietly upgraded."""

    status: Status
    action: Transition | None = None
    confidence: float | None = None
    rationale: str = ""
    cited_facts: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    problem: str | None = None
    model: str | None = None
    facts: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.action is None:
            return ""
        return self.action.value.replace("dispose_", "").replace("_", " ")


def case_facts(case_file: CaseFile, *, now: datetime | None = None) -> dict[str, str]:
    """The only facts the model is allowed to reason from, keyed for citation."""
    now = now or utc_now()
    case = case_file.case
    minutes = (
        (now - case.original_settled_at).total_seconds() / 60 if case.original_settled_at else None
    )
    facts = {
        "rail": case.rail.value,
        "direction": case.direction.value,
        "amount_usd": f"{case.amount_cents / 100:,.2f}",
        "reason": case.reason.value,
        "arrived_via": case.channel.value,
        "state": case_file.state.value,
        "deadline_authority": case_file.deadline.authority.value,
        "deadline_due_at": case_file.deadline.due_at.isoformat(timespec="minutes"),
        "deadline_passed": str(case_file.is_breached(at=now)).lower(),
        "hours_to_deadline": f"{case_file.deadline.remaining(at=now) / 3600:.1f}",
        "steps_so_far": ", ".join(s.transition.value for s in case_file.history) or "none",
        "allowed_actions": ", ".join(sorted(t.value for t in case_file.legal_transitions())),
    }
    if minutes is not None:
        facts["minutes_since_settlement"] = f"{minutes:.0f}"
        facts["estimated_share_still_in_account"] = f"{expected_share_remaining(minutes):.0%}"
        facts["share_estimate_basis"] = "UK mule-account data (RUSI 2025); population average"
    return facts


def _render(facts: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in facts.items())


def recommend(
    case_file: CaseFile,
    *,
    model: StructuredModel | None = None,
    now: datetime | None = None,
) -> Recommendation:
    """Ask the model for a suggestion and check it. Never raises for model failure."""
    facts = case_facts(case_file, now=now)
    chosen = model if model is not None else default_model()

    if case_file.is_closed:
        return Recommendation(status="unavailable", problem="case is closed", facts=facts)
    if chosen is None:
        return Recommendation(
            status="unavailable", problem="AI is not configured (no API key)", facts=facts
        )

    try:
        raw = chosen.parse(system=SYSTEM_PROMPT, user=_render(facts), schema=ModelRecommendation)
    except AIUnavailableError as down:
        return Recommendation(status="unavailable", problem=str(down), facts=facts)

    return check(raw, case_file, facts=facts, model_name=getattr(chosen, "name", None), now=now)


def check(
    raw: ModelRecommendation,
    case_file: CaseFile,
    *,
    facts: dict[str, str],
    model_name: str | None = None,
    now: datetime | None = None,
) -> Recommendation:
    """The deterministic gate every model suggestion passes through."""
    action = Transition(raw.action)

    def rejected(why: str) -> Recommendation:
        return Recommendation(status="rejected", problem=why, model=model_name, facts=facts)

    if action not in case_file.legal_transitions():
        return rejected(f"suggested {action.value}, which is not allowed from this state")
    if action is Transition.ACKNOWLEDGE_SLA_EXPIRY and not case_file.is_breached(
        at=now or utc_now()
    ):
        return rejected("suggested giving up on a case whose deadline has not passed")

    cited = list(dict.fromkeys(raw.cited_facts))  # duplicates prove nothing
    invented = [f for f in cited if f not in facts]
    if invented:
        return rejected(f"cited facts it was not given: {', '.join(invented[:3])}")
    if not cited:
        return rejected("gave no supporting facts")

    # A number in the rationale must be one the model was given. "The balance
    # is $0" on a case where no balance was supplied is a fact it made up, and
    # a review got exactly that through with a citation to the rail.
    supplied = {
        n.replace(",", "") for v in facts.values() for n in re.findall(r"\d+(?:[.,]\d+)*", v)
    }
    for number in re.findall(r"\d+(?:[.,]\d+)*", raw.rationale):
        if number.replace(",", "") not in supplied:
            return rejected(f"its reasoning uses a number it was not given ({number})")

    return Recommendation(
        status="ok",
        action=action,
        confidence=raw.confidence,
        rationale=raw.rationale.strip(),
        cited_facts=tuple(cited),
        missing_information=tuple(m for m in raw.missing_information if m.strip()),
        model=model_name,
        facts=facts,
    )


def is_disposing(action: Transition | None) -> bool:
    return action in DISPOSITION_OF
