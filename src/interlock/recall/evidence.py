"""The compliance evidence export.

What an institution hands an examiner who asks whether it met the Nacha
ten-banking-day response obligation. No standard artifact for this appears to
exist, so this format is Interlock's own and says so in its own header.

What the chain does and does not defend against
-----------------------------------------------
An earlier version of this docstring claimed that "if we altered a single
historical event, the recomputation fails in the examiner's hands, not ours."
**That was not true, and the claim mattered more than the bug.** The chain is
unkeyed: recomputing every digest forward from an edit takes about a dozen
lines, and anyone who can run this code can do it. That includes the
institution, which is the only adversary an examiner actually cares about.

So, precisely:

- **Detected:** accidental corruption, a partial write, an edit by anyone who
  cannot re-run the chain construction, an entry removed from the middle, a
  payload altered without recomputing, and - since a review found these
  passing - a truncated tail left behind an unedited summary, and any summary
  figure that disagrees with the chain.
- **Not detected:** an institution that edits history and recomputes the whole
  chain before exporting, or drops the newest entries and edits the summary to
  match. Nothing in an unkeyed, self-contained document can prevent either.

Closing that gap needs an anchor outside the institution's control: entries
counter-signed by a key it does not hold, or periodic digests published
somewhere append-only. That is a real piece of work and it is not built here.
Saying so is better than a document that implies otherwise, because a
compliance artifact that overstates its own guarantees is worse than one that
states a modest guarantee accurately.

:func:`verify_export` is the reference implementation of what *is* checked. It
takes a plain dict so an examiner can run it against a file, without access to
the system that produced it and without trusting the ``intact`` flag the
export asserts about itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from interlock.recall.ledger import LedgerEntry, rebuild_chain
from interlock.recall.state import CaseFile
from interlock.schema.audit import digest_payload
from interlock.schema.common import Rail, utc_now
from interlock.schema.rails import profile_for
from interlock.schema.recall import RecallDispositionCode
from interlock.sla.clock import DeadlineAuthority, answered_in_time

EXPORT_FORMAT = "interlock-compliance-evidence/1"
"""This format is ours.

Nacha mandates the response and prescribes no format for evidencing it. The
version tag is in every export so a reader can tell our convention from a
standard, and so it can change without ambiguity.
"""


@dataclass(frozen=True, slots=True)
class ObligationSummary:
    """How one rail's cases fared against their deadlines."""

    rail: Rail
    total_cases: int
    answered_within_window: int
    breached: int
    still_open: int
    deadline_is_binding: bool
    """Whether a rule set these deadlines or Interlock's own policy did.

    Carried per rail because a 100% figure means something very different on
    ACH, where a rule obliges the answer, than on FedNow, where we invented the
    deadline ourselves. An export that reported one number across all rails
    would be claiming compliance with an obligation that does not exist.
    """

    @property
    def compliance_rate(self) -> float | None:
        """Share answered in time, or None where nothing has closed yet."""
        closed = self.answered_within_window + self.breached
        return (self.answered_within_window / closed) if closed else None


def summarise_obligations(cases: list[CaseFile]) -> list[ObligationSummary]:
    """Per-rail compliance, split by whether the deadline is real."""
    summaries: list[ObligationSummary] = []

    for rail in sorted(Rail, key=lambda r: r.value):
        subset = [c for c in cases if c.case.rail is rail]
        if not subset:
            continue

        answered = 0
        breached = 0
        still_open = 0
        for case_file in subset:
            if not case_file.is_closed:
                still_open += 1
            elif case_file.disposition is RecallDispositionCode.SLA_EXPIRED_ACKNOWLEDGED:
                # A breach, unconditionally, whatever the timestamps say.
                #
                # An earlier version classified purely on disposed_at against
                # due_at, and because expiry can be acknowledged at exactly
                # due_at, a case closed with an operator formally recording
                # "we missed the window" was counted as answered in time. A
                # review found an export reading 100% compliance on a set of
                # acknowledged breaches - on the single number a Nacha
                # examiner would look at.
                breached += 1
            elif case_file.disposed_at and answered_in_time(
                case_file.disposed_at, case_file.deadline.due_at
            ):
                answered += 1
            else:
                breached += 1

        summaries.append(
            ObligationSummary(
                rail=rail,
                total_cases=len(subset),
                answered_within_window=answered,
                breached=breached,
                still_open=still_open,
                deadline_is_binding=any(
                    c.deadline.authority is DeadlineAuthority.RAIL_RULE for c in subset
                ),
            )
        )

    return summaries


def build_export(
    *,
    cases: list[CaseFile],
    entries: list[LedgerEntry],
    period_start: datetime,
    period_end: datetime,
    institution_id: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the evidence document.

    Everything a third party needs is in here: the claims, the chain that
    supports them, and the payloads the chain commits to.

    Raises:
        ValueError: if ``cases`` and ``entries`` describe different sets of
            cases. The verifier re-derives every figure from the chain, so a
            summary built from a subset of the cases on it would fail
            verification even though nothing was tampered with - a review
            produced exactly that by passing a filtered case list.
    """
    on_chain = {
        item.payload.get("case_id")
        for item in entries
        if item.entry.event_type.value == "recall_raised"
    }
    summarised = {c.case_id for c in cases}
    if on_chain != summarised:
        raise ValueError(
            f"cases and entries disagree: {len(summarised - on_chain)} case(s) have no "
            f"opening entry and {len(on_chain - summarised)} opened case(s) are missing from "
            f"the summary. Export the whole chain with every case on it."
        )

    problems = rebuild_chain(entries)

    return {
        "format": EXPORT_FORMAT,
        "disclaimer": (
            "This format is Interlock's own. Nacha requires a response within ten banking "
            "days and prescribes no format for evidencing it; no standard artifact for this "
            "purpose was found to exist. Verify with verify_export() or by recomputing the "
            "chain per the algorithm described in verification.method below. "
            "LIMITATION: the chain is unkeyed and self-contained. It detects corruption, "
            "partial writes, removed, reordered or duplicated entries, altered payloads, "
            "and any summary figure that disagrees with the events beneath it. It does NOT "
            "detect (a) an institution that edits its own history and recomputes the chain, "
            "or (b) a tail truncation where the summary is edited to match - every prefix "
            "of a valid chain is itself valid, so dropping the newest entries is invisible "
            "unless the head hash was recorded somewhere outside this file. Both need an "
            "external anchor: entries counter-signed by a key the institution does not "
            "hold, or head digests published somewhere append-only. Neither is implemented."
        ),
        "institution_id": institution_id,
        "generated_at": (generated_at or utc_now()).isoformat(),
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "note": "Informational. The export covers every case on the chain.",
        },
        "obligations": [
            {
                "rail": s.rail.value,
                "total_cases": s.total_cases,
                "answered_within_window": s.answered_within_window,
                "breached": s.breached,
                "still_open": s.still_open,
                "compliance_rate": s.compliance_rate,
                "deadline_is_binding": s.deadline_is_binding,
                "deadline_basis": (
                    profile_for(s.rail).window.provenance.claim
                    if s.deadline_is_binding
                    else "Interlock house policy; no rail rule established. Not an obligation "
                    "the counterparty agreed to."
                ),
                "source": profile_for(s.rail).window.provenance.source_url,
                "source_confidence": profile_for(s.rail).window.provenance.confidence.value,
            }
            for s in summarise_obligations(cases)
        ],
        "disposition_completeness": {
            "closed_cases": sum(1 for c in cases if c.is_closed),
            "closed_without_disposition": sum(
                1 for c in cases if c.is_closed and c.disposition is None
            ),
            "note": (
                "closed_without_disposition is zero by construction, not by observation: "
                "the state machine offers no transition that reaches a closed state without "
                "recording an outcome."
            ),
        },
        "verification": {
            "chain_length": len(entries),
            "intact": not problems,
            "problems": problems,
            "method": (
                "For each entry in order: recompute payload_digest as the SHA-256 of the "
                "payload serialised as JSON with sorted keys and (',',':') separators; "
                "recompute entry_hash as the SHA-256 of the JSON object containing "
                "sequence_number, previous_hash, event_type, actor_institution_id, "
                "correlation_id, payload_digest and recorded_at, also with sorted keys and "
                "those separators. Each entry's previous_hash must equal the prior entry's "
                "entry_hash; the first must be 64 zeroes. Sequence numbers must be "
                "contiguous from zero."
            ),
        },
        "chain": [
            {
                "sequence_number": item.entry.sequence_number,
                "previous_hash": item.entry.previous_hash,
                "entry_hash": item.entry.entry_hash,
                "event_type": item.entry.event_type.value,
                "actor_institution_id": item.entry.actor_institution_id,
                "correlation_id": item.entry.correlation_id,
                "payload_digest": item.entry.payload_digest,
                "recorded_at": item.entry.recorded_at.isoformat(),
                "payload": item.payload,
            }
            for item in entries
        ],
    }


def verify_export(export: dict[str, Any]) -> list[str]:
    """Verify an export; see :func:`_verify`. Never raises on malformed input.

    A third review still found five shapes of input that raised instead of
    being reported. Rather than chase each one, anything the checks below
    trip over becomes a problem in the list: an examiner handed a broken file
    should get a finding, not a traceback.
    """
    if not isinstance(export, dict):
        return [f"export is a {type(export).__name__}, not an object"]
    try:
        return _verify(export)
    except (TypeError, ValueError, AttributeError, KeyError) as broken:
        return [f"export is malformed and could not be fully checked: {broken!r}"]


def _verify(export: dict[str, Any]) -> list[str]:
    """Verify an export from its contents alone.

    The reference implementation of the check described in the export's own
    ``verification.method``. Takes a plain dict so it can run against a file
    an examiner was handed, with no access to the system that produced it and
    no need to trust the ``intact`` flag the export asserts about itself.

    Two rules, both learned the hard way from reviews:

    **Nothing the export says about itself is trusted.** A missing summary
    block is a problem, not a skipped check; an empty chain is checked against
    the claims it would have to support; a declared length is compared rather
    than believed.

    **Every headline figure is re-derived, not just the total.** An earlier
    version checked only that answered plus breached matched the number of
    dispositions - which nobody would falsify - and never checked the split,
    which is exactly what an institution would falsify. Turning breaches into
    100% compliance passed verification.

    What this still cannot catch is stated in the export's disclaimer: a
    recomputed chain, and a tail truncation with the summary edited to match.
    """
    problems: list[str] = []

    if export.get("format") != EXPORT_FORMAT:
        return [f"unexpected format {export.get('format')!r}"]

    chain = export.get("chain")
    if not isinstance(chain, list):
        return ["export has no chain list"]

    verification = export.get("verification")
    declared_length = verification.get("chain_length") if isinstance(verification, dict) else None
    if not isinstance(declared_length, int) or isinstance(declared_length, bool):
        problems.append(
            f"chain_length must be an integer, got {declared_length!r}; the export does not "
            f"declare how long its own chain is"
        )
    elif len(chain) != declared_length:
        problems.append(
            f"chain holds {len(chain)} entries but the export declares {declared_length}; "
            f"entries were removed or added"
        )

    problems.extend(_verify_links(chain))
    problems.extend(_summary_disagrees_with_chain(export, chain))
    return problems


def _verify_links(chain: list[Any]) -> list[str]:
    """Recompute every digest and link. Malformed links are reported, not raised."""
    import hashlib
    import json as _json

    problems: list[str] = []
    previous_hash = "0" * 64
    expected_sequence = 0
    required = (
        "sequence_number",
        "previous_hash",
        "entry_hash",
        "event_type",
        "actor_institution_id",
        "correlation_id",
        "payload_digest",
        "recorded_at",
        "payload",
    )

    for position, link in enumerate(chain):
        if not isinstance(link, dict):
            problems.append(f"link at position {position} is not an object")
            continue
        missing = [k for k in required if k not in link]
        if missing:
            problems.append(f"link at position {position} is missing {', '.join(missing)}")
            continue

        seq = link["sequence_number"]
        if seq != expected_sequence:
            problems.append(
                f"#{seq}: sequence gap - expected {expected_sequence}; an entry was "
                f"removed, duplicated or reordered"
            )
            if isinstance(seq, int):
                expected_sequence = seq
        expected_sequence += 1

        try:
            recomputed_payload = hashlib.sha256(
                _json.dumps(
                    link["payload"], sort_keys=True, separators=(",", ":"), default=str
                ).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError):
            problems.append(f"#{seq}: payload cannot be serialised")
            continue
        if recomputed_payload != link["payload_digest"]:
            problems.append(f"#{seq}: payload does not match its committed digest")

        material = _json.dumps(
            {
                "sequence_number": seq,
                "previous_hash": link["previous_hash"],
                "event_type": link["event_type"],
                "actor_institution_id": link["actor_institution_id"],
                "correlation_id": link["correlation_id"],
                "payload_digest": link["payload_digest"],
                "recorded_at": link["recorded_at"],
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if hashlib.sha256(material.encode("utf-8")).hexdigest() != link["entry_hash"]:
            problems.append(f"#{seq}: entry_hash does not match its contents - it was modified")

        if link["previous_hash"] != previous_hash:
            problems.append(f"#{seq}: previous_hash does not match the prior entry")

        previous_hash = link["entry_hash"]

    return problems


def _derive_from_chain(chain: list[Any]) -> tuple[dict[str, dict[str, int]], int, list[str]]:
    """Rebuild the per-rail figures from the chain payloads alone.

    Uses :func:`~interlock.sla.clock.answered_in_time`, the same rule the
    summary was built with, so the two cannot disagree about which side of a
    deadline a case fell on.
    """
    problems: list[str] = []
    opened: dict[str, dict[str, Any]] = {}
    closed: dict[str, dict[str, Any]] = {}

    for link in chain:
        if not isinstance(link, dict):
            continue
        payload = link.get("payload")
        if not isinstance(payload, dict) or not payload.get("case_id"):
            continue
        case_id = payload["case_id"]
        if link.get("event_type") == "recall_raised":
            opened[case_id] = payload
        elif payload.get("disposition"):
            if case_id in closed:
                problems.append(f"{case_id} is disposed twice on the chain")
            closed[case_id] = payload

    for case_id in closed:
        if case_id not in opened:
            problems.append(f"{case_id} is disposed on the chain but never opened on it")

    per_rail: dict[str, dict[str, int]] = {}
    for case_id, opening in opened.items():
        rail = opening.get("rail")
        bucket = per_rail.setdefault(
            rail, {"total_cases": 0, "answered_within_window": 0, "breached": 0, "still_open": 0}
        )
        bucket["total_cases"] += 1

        closing = closed.get(case_id)
        if closing is None:
            bucket["still_open"] += 1
            continue

        if closing.get("disposition") == RecallDispositionCode.SLA_EXPIRED_ACKNOWLEDGED.value:
            bucket["breached"] += 1
            continue

        try:
            at = datetime.fromisoformat(closing["at"])
            due = datetime.fromisoformat(closing["deadline_due_at"])
            in_time = answered_in_time(at, due)
            arrived = datetime.fromisoformat(opening["received_at"])
            predates = at < arrived
        except (KeyError, TypeError, ValueError):
            problems.append(f"{case_id}: disposition has no readable time or deadline")
            bucket["breached"] += 1
            continue
        if predates:
            problems.append(f"{case_id}: disposed before the request arrived")

        if in_time:
            bucket["answered_within_window"] += 1
        else:
            bucket["breached"] += 1

        # The entry records its own verdict at the moment it was written. If
        # that disagrees with the times on the same entry, one of them was
        # edited after the fact.
        recorded_verdict = closing.get("breached_at_transition")
        if recorded_verdict is not None and recorded_verdict != str(not in_time):
            problems.append(
                f"{case_id}: entry records breached_at_transition={recorded_verdict!r} but its "
                f"own times say {not in_time}"
            )

    return per_rail, len(closed), problems


def _summary_disagrees_with_chain(export: dict[str, Any], chain: list[Any]) -> list[str]:
    """Compare every claimed figure against what the chain actually shows."""
    per_rail, closed_count, problems = _derive_from_chain(chain)
    binding = _binding_rails(chain)

    completeness = export.get("disposition_completeness")
    if not isinstance(completeness, dict):
        problems.append("export omits disposition_completeness; its claims cannot be checked")
    else:
        if completeness.get("closed_cases") != closed_count:
            problems.append(
                f"export claims {completeness.get('closed_cases')!r} closed cases; the chain "
                f"records {closed_count} dispositions"
            )
        if completeness.get("closed_without_disposition") != 0:
            problems.append(
                f"export reports {completeness.get('closed_without_disposition')!r} closed "
                f"cases without a disposition while asserting the count is zero by construction"
            )

    obligations = export.get("obligations")
    if not isinstance(obligations, list):
        problems.append("export omits obligations; its compliance claims cannot be checked")
        return problems

    claimed_rails: set[str] = set()
    for obligation in obligations:
        if not isinstance(obligation, dict):
            problems.append("an obligations entry is not an object")
            continue
        rail = obligation.get("rail")
        claimed_rails.add(rail)
        derived = per_rail.get(
            rail, {"total_cases": 0, "answered_within_window": 0, "breached": 0, "still_open": 0}
        )

        for field_name, value in derived.items():
            claimed = obligation.get(field_name)
            if type(claimed) is not int or claimed != value:
                problems.append(
                    f"{rail}: export claims {field_name}={obligation.get(field_name)!r}; the "
                    f"chain shows {value}"
                )

        closed = derived["answered_within_window"] + derived["breached"]
        expected_rate = (derived["answered_within_window"] / closed) if closed else None
        claimed_rate = obligation.get("compliance_rate")
        rate_matches = (
            claimed_rate is None
            if expected_rate is None
            else isinstance(claimed_rate, int | float) and abs(claimed_rate - expected_rate) < 1e-9
        )
        if not rate_matches:
            problems.append(
                f"{rail}: export claims compliance_rate={claimed_rate!r}; the chain supports "
                f"{expected_rate!r}"
            )

        problems.extend(_provenance_disagrees(rail, obligation, binding.get(rail, False)))

    for rail in set(per_rail) - claimed_rails:
        problems.append(f"{rail}: cases on the chain are missing from the obligations summary")

    return problems


def _binding_rails(chain: list[Any]) -> dict[str, bool]:
    """Per rail, whether any case on the chain opened under a rail rule."""
    binding: dict[str, bool] = {}
    for link in chain:
        if isinstance(link, dict) and link.get("event_type") == "recall_raised":
            payload = link.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("rail"), str):
                binding[payload["rail"]] = binding.get(payload["rail"], False) or (
                    payload.get("deadline_authority") == DeadlineAuthority.RAIL_RULE.value
                )
    return binding


def _provenance_disagrees(rail: Any, obligation: dict[str, Any], binding: bool) -> list[str]:
    """Whether a deadline is binding, and on what source, is checked too.

    The third review relabelled a missed Nacha obligation as house policy and
    the export verified: only the counts were compared. Binding comes from the
    chain's own opening entries; the source fields from the rail matrix the
    export claims to cite.
    """
    problems: list[str] = []
    if obligation.get("deadline_is_binding") is not binding:
        problems.append(
            f"{rail}: export claims deadline_is_binding="
            f"{obligation.get('deadline_is_binding')!r}; the chain shows {binding}"
        )
    try:
        provenance = profile_for(Rail(rail)).window.provenance
    except ValueError:
        return [*problems, f"{rail!r} is not a known rail"]
    expected = {
        "source": provenance.source_url,
        "source_confidence": provenance.confidence.value,
        "deadline_basis": provenance.claim
        if binding
        else "Interlock house policy; no rail rule established. Not an obligation "
        "the counterparty agreed to.",
    }
    for name, value in expected.items():
        if obligation.get(name) != value:
            problems.append(f"{rail}: {name} does not match the rail matrix")
    return problems


def digest_of(payload: dict[str, Any]) -> str:
    """Re-exported so a caller checking one payload need not import the schema."""
    return digest_payload(payload)
