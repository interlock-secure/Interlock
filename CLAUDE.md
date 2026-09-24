# Interlock - agent working notes

Read this before changing anything. It is the contract for anyone working in
this repo, human or agent.

## What this is

A **cross-rail recall and return operations system** for US banks. It handles the
life of a "please send that money back" request - inbound or outbound, over
FedNow, RTP, ACH or wire - from arrival to a recorded ending.

It is **not** a fraud detection model, **not** a Reg E dispute manager, and
**not** a payment network. Detection vendors stop when money moves; Interlock
starts there. Dispute vendors run the victim's Reg E clock; Interlock runs the
interbank recovery race alongside it.

Current strategy and milestones: `docs/BUILD_PLAN.md` (v2.0). Read it before
changing anything structural - it explains why v1.0's "signal network" framing
was replaced, and the reasoning matters more than the conclusion.

The specification is `docs/PRD_v2_0.docx`. Where it and BUILD_PLAN.md disagree,
the PRD wins until we agree to change it, and the PRD gets updated in the same
PR. `docs/PRD_v1_1_superseded.docx` is kept for history only - it describes the
signal-network framing v2.0 replaced, and nothing should be built from it.

## Three rules that are not negotiable

1. **No recall case closes without a recorded disposition.** SLA expiry is
   itself a disposition requiring acknowledgement, not an absence of one. If you
   can find a code path where a case goes quiet, that is a bug regardless of what
   the tests say. This is the product's core claim, not a nicety: Nacha has
   required a response within ten banking days since April 2025 and prescribes no
   format for it.

2. **Never return "low risk" when we mean "we don't know."** A timeout, an
   unreachable counterparty, or a missing record must surface as an explicit
   `unavailable` or `no_signal` state. Presenting absence of evidence as evidence
   of absence is the exact failure this system exists to remove.

3. **Rail rules live in one place and carry their provenance.** Every deadline
   and obligation comes from `schema/rails.py`, with a source URL and a
   confidence level. Several are genuinely unverified because the primary sources
   are gated. Code that depends on an unverified entry must fail loudly rather
   than guess. Never hardcode a rail rule anywhere else.

## Architecture

```
   every channel a request can arrive on
   (camt.056, ACH R06, Fed ERS case, email, phone note)
        |
   adapters  (src/interlock/adapters/)  - normalise to one case shape
        |
   sla + recall  (src/interlock/sla/, src/interlock/recall/)
        |         clocks, state machine, disposition ledger
        |
   +----+--------+---------+
   |             |         |
 triage       console    evidence export
 (ranking)               (hash-chained)
```

`src/interlock/schema/` is the contract everything else codes against.
**Do not change it casually.** Any change to a published model requires a
version bump and a passing round-trip test against the previous version.

## Money

All monetary values are **integer cents**, typed as `int`. Never float, never
`Decimal` in the wire schema. A field holding dollars is a bug.

## Privacy

Account identifiers are hashed before they leave an institution. No name,
address, balance, or transaction detail crosses an institutional boundary. If a
model field could carry PII, it does not belong in `schema/`.

## Commands

```bash
uv sync --all-extras            # install
uv run pytest                   # tests, with coverage gate at 70%
uv run ruff check --fix .       # lint
uv run ruff format .            # format
uv run uvicorn interlock.api.app:app --reload   # run locally
# Hosted: render.yaml deploys the same app to Render (New > Blueprint)
```

CI runs `ruff check`, `ruff format --check`, and `pytest`. All three must pass
before a PR can merge. Do not push directly to `main`; it is protected.

## Budgets that are asserted in tests, not just documented

- Case API p99 **under 300ms**. Not a rail constraint - recall runs on a
  banking-day clock, not a five-second one - but an operator working a decaying
  queue under time pressure should never wait on the tool.
- Every state change writes a hash-chained audit entry. Zero exceptions. The
  verifier in `src/interlock/schema/audit.py` must be able to detect any edit.
  This chain is the regulatory evidence artifact, not just a log.

## Working style for agents

- Small files, explicit names. `recall/sla.py`, not `recall/utils.py`.
- Tests before implementation for anything in `recall/` - the state machine is
  the differentiating component and a subtle bug there hides a long time.
- Never rewrite a file's contents from a truncated read. Read it fully first.
- If a change touches `schema/`, say so loudly in the PR description.
