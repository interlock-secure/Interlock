"""AI reply drafting: the message back to the requesting bank, for a person to send.

Once an outcome is recorded, the counterparty needs to hear it. On ACH the
answer is mandatory and Nacha prescribes no format; on the instant rails the
formal answer is a camt.029, which Interlock already produces deterministically
from the case. What is left for the model is the human-readable note that goes
with it - the part operators currently type by hand.

Checks on every draft, in code:

- **It must state the recorded outcome, the case id and the exact amount.**
  A reply that says "we have returned the funds" on a case recorded as
  declined is the most damaging thing this feature could produce, so the
  draft is compared with the record, not trusted.
- **It must not contain long digit runs that are not in the facts.** An
  account or routing number the model made up, or copied from somewhere it
  should not, is stopped before it reaches the screen.
- **It is never sent.** The console shows it with a copy button. Sending is a
  person's act, through the institution's own channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from interlock.ai.client import AIUnavailableError, StructuredModel, default_model
from interlock.recall.state import CaseFile

SYSTEM_PROMPT = """You draft a short, professional reply from one US bank's
recall operations team to another bank that asked for a payment to be
returned. The facts of the case and the outcome already decided are given as
key: value lines. Write a subject line and a body of 60 to 140 words.

Rules:
- State the outcome exactly as given. Do not soften, upgrade or change it.
- Include the case_id and the amount exactly as given.
- If a stated_basis is given, include it in plain words.
- Do not include account numbers, routing numbers, customer names or any
  detail not in the facts.
- Do not promise anything beyond the recorded outcome.
- Do not add dates or any other numbers beyond those in the facts.
- Sign off as "Recall Operations"."""


class ModelDraft(BaseModel):
    subject: str = Field(max_length=120)
    body: str = Field(max_length=1400)


Status = Literal["ok", "rejected", "unavailable"]

OUTCOME_WORDS = {
    "funds_returned": ("returned", "return"),
    "partial_return": ("partial",),
    "funds_frozen": ("frozen", "freeze"),
    "insufficient_funds": ("insufficient", "no funds", "not available", "unavailable"),
    "account_holder_disputes": ("disput",),
    "declined_with_reason": ("declin", "unable to return", "cannot return"),
    "sla_expired_acknowledged": ("window", "deadline", "expired", "time"),
}
"""Words that show a draft actually states the recorded outcome."""


MONEY_MOVED = re.compile(
    r"\b(returned|refunded|sent back|reversed|credited back|will be returned|"
    r"(?:is|are|will be|has been|have been|were|was) (?:now )?(?:being )?returned)\b"
)
NEGATION = re.compile(
    r"\b(not|never|no|unable to|cannot|can't|could not|couldn't|did not|didn't|"
    r"will not|won't|has not|have not|hasn't|haven't|were not|was not)\b[^.;:!?]{0,24}$"
)
"""A money-moved phrase counts as denied only when a negation sits just before
it in the same clause. The first version matched two fixed phrasings, and a
review got "funds have now been returned", "were returned today" and "will be
returned shortly" through on a declined case."""

MOVED_OUTCOMES = {"funds_returned", "partial_return"}


def _affirms_money_moved(lowered: str) -> bool:
    for match in MONEY_MOVED.finditer(lowered):
        if not NEGATION.search(lowered[: match.start()]):
            return True
    return False


def _denies_money_moved(lowered: str) -> bool:
    for match in MONEY_MOVED.finditer(lowered):
        if NEGATION.search(lowered[: match.start()]):
            return True
    return bool(re.search(r"\b(not|cannot|unable to|could not|did not) return\b", lowered))


@dataclass(frozen=True, slots=True)
class ReplyDraft:
    status: Status
    subject: str = ""
    body: str = ""
    problem: str | None = None
    model: str | None = None


def reply_facts(case_file: CaseFile) -> dict[str, str]:
    if case_file.disposition is None:
        raise ValueError("a reply is drafted only after an outcome is recorded")
    return {
        "case_id": case_file.case_id,
        "rail": case_file.case.rail.value,
        "original_payment_reference": case_file.case.original_payment_reference,
        "amount_usd": f"${case_file.case.amount_cents / 100:,.2f}",
        "outcome": case_file.disposition.value,
        "stated_basis": case_file.disposition_reason or "",
        "recorded_by_team": "Recall Operations",
    }


def draft_reply(case_file: CaseFile, *, model: StructuredModel | None = None) -> ReplyDraft:
    """Draft the counterparty reply for a closed case. Never raises for model failure."""
    if case_file.disposition is None:
        return ReplyDraft(status="unavailable", problem="record an outcome first")

    chosen = model if model is not None else default_model()
    if chosen is None:
        return ReplyDraft(status="unavailable", problem="AI is not configured (no API key)")

    facts = reply_facts(case_file)
    try:
        raw = chosen.parse(
            system=SYSTEM_PROMPT,
            user="\n".join(f"{k}: {v}" for k, v in facts.items()),
            schema=ModelDraft,
        )
    except AIUnavailableError as down:
        return ReplyDraft(status="unavailable", problem=str(down))

    return check_draft(raw, facts, model_name=getattr(chosen, "name", None))


def check_draft(raw: ModelDraft, facts: dict[str, str], *, model_name: str | None) -> ReplyDraft:
    text = f"{raw.subject}\n{raw.body}"
    lowered = re.sub(r"\s+", " ", text.lower())

    def rejected(why: str) -> ReplyDraft:
        return ReplyDraft(status="rejected", problem=why, model=model_name)

    if facts["case_id"] not in text:
        return rejected("the draft does not quote the case id")
    if facts["amount_usd"] not in text:
        return rejected(f"the draft does not state the amount as {facts['amount_usd']}")
    if not any(word in lowered for word in OUTCOME_WORDS.get(facts["outcome"], ())):
        return rejected(f"the draft does not state the recorded outcome ({facts['outcome']})")

    # The outcome, both ways round: money that did not move must not be said
    # to have moved, and money that did must not be denied.
    if facts["outcome"] not in MOVED_OUTCOMES and _affirms_money_moved(lowered):
        return rejected("the draft says funds were returned; the record says not")
    if facts["outcome"] in MOVED_OUTCOMES and _denies_money_moved(lowered):
        return rejected("the draft denies a return the record shows was made")

    # Numbers: any run of eight or more digits, allowing spaces and dashes
    # between groups, must be exactly one of the numbers in the facts. Partial
    # matches ("91000019887" cut from a trace) no longer count.
    allowed = {re.sub(r"\D", "", v) for v in facts.values()} - {""}
    for run in re.findall(r"\d(?:[\d\s-]*\d)?", text):
        digits = re.sub(r"\D", "", run)
        if len(digits) >= 8 and digits not in allowed:
            return rejected("the draft contains a number that is not in the case facts")

    return ReplyDraft(
        status="ok", subject=raw.subject.strip(), body=raw.body.strip(), model=model_name
    )
