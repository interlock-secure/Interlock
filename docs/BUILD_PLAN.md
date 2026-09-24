# Interlock - Strategy and Build Plan v2.0

**Status:** supersedes v1.0 (commit `49a30ad`). The engineering in v1.0 was sound. The
product strategy was not, and this document explains why and replaces it.

**All milestones M0 through M10 are complete.** 397 tests, 92% coverage, lint and
format clean. Section 7 keeps each milestone's acceptance criteria as written, so
the plan can still be read as the contract they were built against; what actually
happened, including the defects the criteria caught, is in `docs/BUILD_LOG.md`.

**Audience:** any agent or engineer picking this repo up cold, with no prior context.

---

## 0. If you are an agent starting here

Read in this order, and do not skip:

1. This document, sections 1 through 5. The strategy is the part most likely to be
   got wrong, because the engineering looks obvious once the strategy is fixed.
2. `CLAUDE.md` - the working contract. Note that rule 3 has been promoted from a
   design preference to the product's core claim.
3. Section 6 below - what already exists and what is being reworked.
4. The single milestone you are being asked to build. Not the rest.

Then stop and confirm scope before writing code. Section 7 lists, for every
milestone, the questions that must be answered before it starts. That gate is not
ceremony. Two of the four defects in section 6.3 were caused by building before the
success criterion was agreed.

---

## 1. The honest verdict on v1.0

v1.0 described Interlock as a cross-institution network for exchanging fraud signals
before a payment settles, with recall as a secondary component.

**That framing had a fatal go-to-market problem and a worsening competitive one.**

**The adoption problem.** A signal-exchange network is worth nothing to the first
participant. Bank one installs it and gets zero signals back, because there is nobody
to ask. Value appears somewhere around participant twenty. Nothing in the design
created a reason for participant one to show up, and no amount of engineering quality
substitutes for that. A protocol without a consortium is a document.

**The competitive problem.** The signal-sharing space is being closed off in real
time. Feedzai opened a federated scoring network across a claimed $9 trillion in
transactions in June 2026, explicitly marketed as delivering value "from day one"
without a consortium build-out. Early Warning Services runs a national shared database
across roughly 2,100 institutions. The Clearing House and Visa are both moving.
Entering that fight with a two-person team is not a differentiated position, it is a
worse-funded copy.

**What survives.** The recall half of v1.0 was not the secondary component. It was the
product. Rule 3 in `CLAUDE.md` - *no recall request closes without a recorded
disposition* - turns out to be a live US regulatory obligation with, as far as the
research below could establish, no software product behind it.

So v2.0 inverts the emphasis. Interlock is a **cross-rail recall and return operations
system**. The network is an upgrade it grows into, not a precondition for it working.

---

## 2. The product

### 2.1 The gap, in one table

Every US payment rail has a way to ask for money back. No US rail has both a machine-
readable format for the answer *and* an enforceable obligation to give one.

| Rail | Structured disposition message | Response obligation | Window |
|---|---|---|---|
| **ACH** | **None.** Nacha states the method is flexible - portal, phone, etc. | **Yes, mandatory** | **10 banking days, even if refusing** |
| **RTP** | Yes - camt.029 | Yes | 10 banking days, *fraud claims reportedly exempt* [UNVERIFIED] |
| **FedNow** | Yes - camt.029 | **No.** Operating Circular 8 §9.8.3 says participants "should" respond | Not established [UNVERIFIED] |
| **Fedwire** (since Jul 2025) | Yes - camt.029 | "Should" | None stated |

Read the first row against the second. **ACH has the strongest obligation and no
format. The instant rails have the format and the weakest obligation.** A bank
operating across all four is running four incompatible processes, three of which have
no enforceable deadline, for the same business event: *my customer was tricked, please
send the money back*.

Three specific findings sharpen this:

- **Nacha, effective 1 April 2025:** an RDFI "must advise the ODFI of its decision or
  the status of the request within ten (10) banking days," and explicitly, this holds
  "regardless of whether the RDFI complies." Silence is a rules violation. Nacha does
  not prescribe a format for that answer.
- **Nacha, effective 1 October 2024:** ODFIs may request a return "for any reason,"
  lifting the prior restriction to erroneous or unauthorised entries, and "False
  Pretenses" entered the Rules as a defined term. Scam cases became formally in scope.
- **Federal Reserve, FDIC and OCC, joint RFI, 16 June 2025:** payments fraud data is
  "currently collected in an incomplete, non-standardized, ad hoc, and fragmented way,"
  and the agencies report "complaints from supervised institutions regarding challenges
  in resolving disputes about liability." The RFI asks about fraud *data*
  standardisation. It does not yet ask about return and recall messaging.

### 2.2 What Interlock is

A system that sits at a single bank and runs the whole life of an inbound or outbound
return-of-funds request, across every rail, to a recorded ending.

```
   inbound requests                                            outbound requests
   (another bank wants money back)                   (our customer was scammed)
          |                                                         |
   camt.056 (FedNow/RTP/Fedwire)                            same channels,
   ACH R06 request + Nacha portal LOI                       outbound direction
   Fed Exception Resolution Service case                            |
   email / phone / secure message  --------+                        |
                                           |                        |
                                    +------v------------------------v----+
                                    |        INTAKE + NORMALISE          |
                                    |  every channel -> one case shape   |
                                    +------------------+-----------------+
                                                       |
                                    +------------------v-----------------+
                                    |          SLA ENGINE                |
                                    |  per-rail clock on banking-day     |
                                    |  calendar; nothing runs silent     |
                                    +------------------+-----------------+
                                                       |
                                    +------------------v-----------------+
                                    |          TRIAGE                    |
                                    |  rank by recoverable dollars per   |
                                    |  analyst-minute, not by arrival    |
                                    +------------------+-----------------+
                                                       |
                                    +------------------v-----------------+
                                    |     DISPOSITION LEDGER             |
                                    |  hash-chained; no case closes      |
                                    |  without a terminal outcome        |
                                    +------------------+-----------------+
                                                       |
                                      +----------------+----------------+
                                      |                                 |
                              emit camt.029 / R06              audit + regulator
                              back over the right rail          evidence export
```

**What it is not**, and these are load-bearing exclusions:

- **Not a fraud detection model.** Detection is solved, well funded and defended.
  Interlock activates *after* money has moved, which is precisely where every detection
  vendor stops.
- **Not a Reg E dispute manager.** Quavo, Pega and FINBOA own the victim-side claim
  workflow and its Reg E and Reg Z clocks. Interlock handles the interbank recovery
  race that runs in parallel and, today, mostly by phone.
- **Not a payment network.** It speaks the rails' existing messages. It does not
  clear, settle or route money.

### 2.3 The wedge: why bank one installs it

This is the question v1.0 could not answer and v2.0 must.

A bank installing Interlock on day one, with zero other participants, gets:

1. **One queue instead of four.** Today a FedNow return request, an ACH R06 request,
   a Fed ERS case and a "my customer was scammed" email arrive in four different
   places, often four different teams. Interlock normalises them into one case model
   with one queue.
2. **Nacha compliance evidence.** The 10-banking-day response obligation is live and
   mandatory. Interlock puts a clock on every request and produces a hash-chained,
   exportable record that the answer was given in time. There is currently no standard
   artifact for proving this.
3. **Triage against a decaying asset.** Published UK data on mule-account behaviour
   (RUSI, July 2025, Lloyds transaction data) found roughly 28% of value left mule
   accounts within 15 minutes and about 53% within an hour, with under 15% remaining
   after 24 hours. If that curve is even directionally right in the US, then working a
   queue in arrival order destroys most of its recoverable value. Ranking the queue is
   worth real money on day one with no counterparties at all.

None of those three require another institution to have installed anything. That is
the whole point.

### 2.4 The network: what it grows into

When two banks both run Interlock, their requests stop going by email and start going
machine to machine, with a structured disposition coming back. That is the v1.0 vision,
reached by a route that does not require it to exist first.

This is deliberately the *last* milestone and is explicitly not required for the
product to be worth building. If it never happens, Interlock is still a working
product. Design every earlier milestone so this is true.

---

## 3. Why this is different from everything in the market

Research findings, with the caveat that absence of a marketed product is weaker
evidence than presence of one. Where a claim rests on not finding something, it is
flagged.

**Detection vendors do not do this.** Feedzai, Featurespace (acquired by Visa,
completed December 2024), NICE Actimize, BioCatch, Socure, Sardine, Unit21, Hawk AI and
DataVisor all sell scoring, alerting and intra-bank case management. None was found to
handle interbank post-fraud recall. NICE Actimize's Claims and Investigations product
uses the word "recovery" without defining its scope - treat as unconfirmed rather than
ruled out.

**Dispute vendors do not do this, and specifically do not do FedNow.** Quavo is the
most rail-complete US dispute vendor found, covering cards, ACH, wire, Zelle, RTP, ATM,
checks and bill pay - and **FedNow appears in neither its product page nor its news
archive through July 2026** [inference from absence, not a vendor denial]. Pega Smart
Dispute covers Zelle and ACH but not RTP or FedNow. Fiserv Dispute Expert is cards.
Jack Henry resells FINBOA for Reg E. All of them begin at the victim bank's Reg E
clock, which is a different job from the interbank recovery race.

**The networks are closed or are plumbing.** Early Warning Services is bank-owned and
its Zelle clawback reportedly still runs on manual bank-to-bank courtesy requests
through a portal. The Clearing House owns the RTP message standard and told federal
regulators in writing, in September 2025, that bank-to-bank claims "are often not
efficient or effective, particularly for smaller-dollar-value claims," asking for "an
automated mechanism." The Federal Reserve's Exception Resolution Service expanded from
ACH to FedNow by 31 October 2025, but does not cover RTP, Zelle or wires. Nacha's
Secure Exchange moves standardised **PDF** letters of indemnity - documents, not data.
**Nothing found spans rails.**

**The category is proven - just not here.** This is the strongest available evidence
that the idea is right:

- **Mastercard A2A Protect "Recover"** provides "a uniform procedure for banks to
  resolve disputes and recover funds." Announced July 2025. **United Kingdom first. No
  US timeline announced.**
- **Salv Bridge** runs interbank fraud investigation and fund recovery across 16-plus
  EU and UK jurisdictions with 100-plus institutions, claiming roughly 80% recovery
  rates [vendor-reported, unaudited]. **No US presence.**

Meanwhile the US comparison: the FBI's Recovery Asset Team froze $679 million of
$1.163 billion attempted in 2025, a 58% success rate - against $20.877 billion in total
reported IC3 losses. **Roughly 3.3% of reported losses even reach the one mechanism
that works.**

So the differentiation claim is not "nobody thought of this." It is: **the product
exists, it works, and it is not in the United States.** That is a far more defensible
thing to say in an interview than claiming novelty.

**Honest caveat to carry everywhere:** Mastercard could announce US availability at any
time, and the ASC X9 Payment Fraud Forum first convened on 10 September 2026 with the
Federal Reserve, The Clearing House and the US Faster Payments Council in the room.
This window is open now. It is not open indefinitely, and the plan should say so rather
than pretend to a durable moat it does not have.

---

## 4. Why now

- **Nacha's fraud monitoring rules are in force.** Phase 1 landed 20 March 2026 for all
  ODFIs and for RDFIs above 10 million 2023 receipt entries. Phase 2 removed the volume
  thresholds on 19 June 2026 - practically 22 June, since the 19th was a holiday.
  **Every RDFI is now covered.** The RDFI duty is narrower than general fraud
  monitoring: it is scoped to *credit entries* suspected unauthorised or authorised
  under False Pretenses.
- **The mandatory response obligation is live.** Ten banking days, since April 2025.
- **Fedwire joined the ISO 20022 world on 14 July 2025**, adding camt.056 and camt.029.
  A fourth rail now has the format and no obligation. Note for implementation: the Fed
  warns that using camt.110 for this "may cause a rejection by a Fedwire receiver."
- **FedNow is at meaningful scale.** Roughly 1,725 institutions as of Q1 2026 per the
  Richmond Fed; 4,997,811 transactions worth $274.7 billion in Q2 2026. RTP reports
  1,357 participants as of August 2026 and a $10 million per-transaction limit since
  February 2025.
- **Federal reimbursement rules are not coming.** The CFPB's Zelle suit was dismissed
  with prejudice on 5 March 2025, and its proposed interpretive rule on emerging
  payment mechanisms was withdrawn on 15 May 2025. US law still gives consumers no
  reimbursement right for authorised payments.

That last point matters more than it looks, and cuts **for** this product rather than
against it. The UK route was liability first, then tooling - the PSR mandate took
effect in October 2024 and Mastercard built Recover in response. The US has the fraud
volume without the forcing function on consumer reimbursement, but it now *does* have
an operational forcing function in the Nacha rules. The pressure is on the process, not
the payout. Interlock is a process product.

---

## 5. The AI, honestly

This is an AI product management portfolio piece, so the AI has to be real, and it has
to have a decision attached. Three components, in descending order of confidence.

**5.1 Recoverability ranking (core).** Given a case - rail, minutes elapsed since the
original payment settled, amount, receiving institution's historical response
behaviour, hour of day, day of week, channel it arrived on - predict the probability
that funds are still recoverable.

The decision it drives: an analyst has a queue of several hundred cases and capacity
for a handful in the next hour. Which ones?

Evaluated on **dollars recovered per analyst-hour under a fixed capacity constraint**,
against three baselines: arrival order (what banks do now), largest-amount-first (the
obvious heuristic), and a perfect-knowledge oracle (the ceiling). AUC is reported but
is not the headline, because this is a ranking-under-constraint problem and calibration
matters more than discrimination.

**5.2 Intake normalisation (high value, honest about limits).** The operational reality
is email and phone. Payments Dive reported a bank operations VP describing registry
contacts as "call-tree-hell" and speaking with 18 representatives at one institution
over four hours to resolve a single issue. An LLM extracting a structured case from
free text is a genuine application, with one non-negotiable design rule: **it proposes,
a human confirms, and it must be able to abstain.** Evaluated on field-level extraction
accuracy against a labelled set, with abstention rate reported separately and no
auto-submission path anywhere in the code.

**5.3 Counterparty response modelling (simple, useful).** Learn each institution's
observed median response time and disposition mix, to set expectations and escalate
before the deadline rather than after. Mostly descriptive statistics. Included because
it is honest about what it is.

**What we are explicitly not claiming.** No accuracy number from this project
transfers to production. The data is synthetic, the recoverability curve is calibrated
from UK data on a different rail set, and no real institution's behaviour is in it. The
models exist to prove the decision architecture is sound and measurable. Any milestone
that starts implying otherwise has failed its review.

---

## 6. What exists, what survives, what changes

### 6.1 Current state, verifiable

Seven commits. 157 tests. Roughly 5,200 lines across `src` and `tests`.

```bash
uv sync --all-extras
uv run pytest                 # 157 pass, coverage gate 70%
uv run ruff check . && uv run ruff format --check .
```

| Module | v1.0 purpose | v2.0 disposition |
|---|---|---|
| `schema/common.py` | money, hashing, risk bands | **Keep.** Integer cents and hashed identifiers are unchanged. `RiskBand` narrows to the signal path. |
| `schema/audit.py` | hash-chained audit | **Keep and promote.** This becomes the regulatory evidence artifact, not an engineering nicety. |
| `schema/pii.py` | allowlist wire guard | **Keep.** Still correct. |
| `schema/versioning.py` | protocol negotiation | **Keep**, needed for M9. |
| `schema/recall.py` | recall disposition codes | **Rework into the core.** Must now map to real rail code sets rather than an invented enum. |
| `schema/signal.py` | pre-settlement signal exchange | **Demote.** Stays for M9, off the critical path. |
| `generator/` | synthetic payments + mules | **Keep as substrate, retarget.** It generates the payment history that recall cases refer back to. The label changes from "is this a mule" to "were these funds recovered." |

### 6.2 The biggest change

v1.0's generator was built to train a detector. v2.0 does not build a detector. The
payment population it produces is still needed - a recall case is meaningless without
the payment it refers to - but the modelling target moves from account classification
to outcome prediction. M5 covers this. Do not delete the generator; retarget it.

### 6.3 Defects found in M2, kept for the record

These are worth preserving because the reasoning is the most transferable thing here,
and because each was found by measuring rather than reading.

| Defect | How it showed up | Fix |
|---|---|---|
| Declared-but-unused velocity dimension | `inbound_per_month` was set per archetype but receivers were drawn uniformly, so every archetype had the same median first-time inbound count. The feature carried zero information. | Weighted receiver selection by `inbound_weight`. |
| Precision hit 1.000 | After that fix, mules were the only accounts that were both young and busy. A single rule separated them perfectly. | `NEWLY_OPENED_SHARE_OF_BUSY_ACCOUNTS = 0.3` - legitimate young busy accounts. |
| The 18-minute fingerprint | Every classic mule swept within the same narrow band because archetypes shared constants. Best single threshold scored F1 0.916. | Per-account parameter draws, plus `AUTO_SWEEP_SHARE_OF_BUSINESSES = 0.18` - real treasury sweep products are indistinguishable from laundering by timing alone. F1 fell to 0.661. |
| Mule population sized as a fixed share | Produced roughly 4 fraud payments per mule, far below any realistic campaign. | Derive mule count from fraud volume instead. |

The general lesson, which applies to every milestone below: **when synthetic data
produces a suspiciously good result, the data is wrong, not the model.** Fix it by
adding legitimate lookalikes, never by weakening the detector.

---

## 7. Milestones

Each milestone below has an objective, build targets, deliverables, an acceptance
checklist, explicit exclusions, and questions that must be answered before it starts.

**The confirmation gate.** No milestone starts until its questions are answered and the
acceptance criteria are agreed. If a milestone is running and its criteria turn out to
be wrong, stop and re-agree rather than quietly redefining success.

**Scope honesty.** This is 7 milestones at 2 to 3 working sessions each. M3, M4, M5 and
M8 are the product; a demo without any one of them does not hold together. M6 is what
makes it an AI portfolio piece. M7 and M9 are genuinely optional, and cutting both
costs the demo very little. **If time is short, cut M7 and M9 first, and say so
explicitly rather than half-building them.**

---

### M3 - Canonical case model and rail adapters

**Objective.** One case shape that every rail's return request normalises into, and
emits back out of without loss.

**Build.**
- `schema/case.py` - the canonical `RecallCase`: rail, direction, original payment
  reference, amount in cents, reason code, requesting and responding institution,
  arrival channel, timestamps, current state.
- `schema/rails.py` - the rail capability matrix from section 2.1, as executable
  policy rather than documentation: per rail, whether a structured disposition exists,
  whether response is obligatory, and the window. Every entry cites its source and
  carries a confidence level. Entries marked UNVERIFIED must fail loudly if code
  depends on them.
- `adapters/camt056.py`, `adapters/camt029.py` - ISO 20022 parse and emit.
- `adapters/ach.py` - R06 request, R10/R11/R17 returns, WSUD reference.
- `adapters/freetext.py` - a deterministic stub with the LLM interface shaped but not
  implemented. M7 fills it in; M3 must work without it.

**Deliverables.** Golden fixtures per rail in `tests/fixtures/rails/`. A round-trip
test proving parse then emit is byte-identical. A rendered version of the capability
matrix in `docs/protocol/rails.md`.

**Acceptance.**
- [ ] A camt.056 parses to a `RecallCase` and re-emits byte-identically.
- [ ] The same for camt.029 and for an ACH R06 request.
- [ ] A free-text email reaches the same `RecallCase` shape through the stub.
- [ ] The capability matrix is the single source of truth - no rail rule is hardcoded
      anywhere else. A grep for rail names outside `rails.py` finds only tests.
- [ ] Every matrix entry carries a source URL and a confidence level.
- [ ] Any code path depending on an UNVERIFIED entry raises rather than guessing.

**Excluded.** No SLA logic, no persistence, no UI, no real LLM call.

**Confirm before starting.**
1. FedNow's camt.056 response window is unverified - it sits in Operating Procedures
   §15.2, which the research could not extract. Do we (a) chase the primary source,
   (b) treat FedNow as house-policy-only with no rail deadline, or (c) block M3 on it?
   **Recommendation: (b), with the gap documented in the matrix.** It is honest, it is
   shippable, and "the rail sets no deadline so we set our own" is itself a finding.
2. Do we model ACH's R06 request as the same object as a camt.056, or as a sibling?
   **Recommendation: same object, different adapter.** The business event is identical.
3. Do we implement Fedwire in M3, or defer? **Recommendation: defer.** It shares
   camt.056 and camt.029 with FedNow, so it is nearly free later, and three rails is
   enough to prove normalisation works.

---

### M4 - SLA engine and disposition ledger

**Objective.** Every case runs a clock against the correct rail's rules, and no case
can reach a closed state without a terminal disposition on the audit chain.

**Build.**
- `sla/calendar.py` - banking-day arithmetic with Federal Reserve holidays. Fiddly,
  and exactly the kind of thing that is wrong in production systems. Note the 19 June
  2026 precedent: a holiday moved an effective date to the 22nd.
- `sla/clock.py` - per-rail deadline computation off the M3 matrix.
- `recall/state.py` - the case state machine. Terminal states only; no state named
  PENDING that can be reached and left forever.
- `recall/ledger.py` - disposition capture appending to the existing hash chain.
- `recall/evidence.py` - regulator-facing export proving response within the window.

**Deliverables.** A property-based test suite, using hypothesis, asserting that no
sequence of legal transitions reaches a closed case without a disposition.

**Acceptance.**
- [ ] Banking-day arithmetic is correct across all Federal Reserve holidays for 2025
      through 2027, including the weekend-adjacent cases.
- [ ] A hypothesis run of at least 10,000 transition sequences finds no path to a
      closed case lacking a disposition.
- [ ] SLA expiry is itself a disposition requiring acknowledgement, per `CLAUDE.md`
      rule 3.
- [ ] The evidence export verifies against the hash chain, and any edit is detected.
- [ ] Every rail's clock is read from the M3 matrix, never hardcoded.

**Excluded.** No model, no UI, no network.

**Confirm before starting.**
1. Persistence: SQLite or Postgres from day one? **Recommendation: SQLite with a
   repository interface.** Free-tier deploy is simpler, and the interface makes the
   swap cheap if M9 needs it. This decision propagates into deploy, so settle it now.
2. What is our house policy deadline for rails that set none? **Recommendation: 24
   hours**, justified by the RUSI decay curve rather than by any rule, and labelled as
   our assumption in the matrix.
3. Should the evidence export target a real regulatory format, or be our own? **Rec:
   our own, clearly labelled**, since no standard artifact was found to exist.

---

### M5 - Recall case generator with outcome labels

**Objective.** Synthetic recall cases whose recovery outcomes are realistic enough to
train and evaluate against, built on the existing payment substrate.

**Build.**
- `generator/recall_cases.py` - derive cases from the existing fraud payments.
- `generator/recovery.py` - the outcome model. Recoverability decays with elapsed
  time, calibrated to the RUSI curve (roughly 28% of value gone in 15 minutes, 53% in
  an hour, under 15% remaining at 24 hours), with every parameter labelled
  `[UK-DERIVED ASSUMPTION]`.
- `generator/counterparties.py` - institutions with heterogeneous behaviour: response
  latency, disposition mix, channel preference. Apply the section 6.3 lesson - if any
  single feature separates recoverable from unrecoverable cleanly, the generator is
  wrong.
- Temporal train and test splits, never random.

**Deliverables.** A calibration report comparing the generated decay curve against the
published one, plus a separability report proving no trivial rule wins.

**Acceptance.**
- [ ] Generated decay curve matches the RUSI anchors within a documented tolerance.
- [ ] No single-feature threshold achieves F1 above 0.75 on the recovery label.
- [ ] Counterparty behaviour is drawn per institution, not shared - the same
      heterogeneity fix as M2.
- [ ] Splits are temporal; a test asserts no train case post-dates any test case.
- [ ] Every assumption is labelled with its provenance and its source.

**Excluded.** No model training. This milestone produces data and its calibration
evidence only.

**Confirm before starting.**
1. Is a UK-derived decay curve acceptable for a US-positioned product? **Recommended
   answer: yes, if labelled loudly**, because no US time-to-dissipation dataset was
   found to exist. Stating that gap is itself a credible finding, and inventing a US
   curve would be worse.
2. How many counterparty institutions? **Recommendation: 40**, enough for per-
   institution statistics to mean something without making the fixtures unwieldy.

---

### M6 - Recoverability model and triage

**Objective.** Rank the queue better than arrival order, measured in recovered dollars
under a capacity constraint.

**Build.**
- `triage/features.py` - feature extraction with a strict no-leakage boundary. Nothing
  known only after the outcome may enter.
- `triage/model.py` - `HistGradientBoostingClassifier`, with a logistic regression
  reported alongside as the simple baseline.
- `triage/policy.py` - ranking under a capacity constraint.
- `eval/harness.py` - the comparison against arrival order, amount-first, and oracle.

**Deliverables.** An evaluation report with calibration curves, capacity sweeps, and
per-rail breakdowns.

**Acceptance.**
- [ ] Beats arrival order on dollars recovered per analyst-hour by a margin larger than
      the seed-to-seed variance across at least 5 seeds.
- [ ] Beats amount-first. If it does not, that is a finding to report, not a failure to
      hide - and it would mean the amount feature dominates, which is worth knowing.
- [ ] Calibration reported, not just discrimination.
- [ ] A leakage test asserts no post-outcome feature reaches the model.
- [ ] The report states plainly that no number transfers to production.

**Excluded.** No LLM. No deep learning - it would be unjustifiable at this data scale
and an interviewer would rightly ask why.

**Confirm before starting.**
1. What analyst capacity do we model? **Recommendation: sweep it** from 5 to 100 cases
   per hour rather than picking one, since the whole value argument depends on scarcity
   and a sweep is more honest than a single flattering point.
2. Do we optimise expected recovered dollars, or probability of recovery?
   **Recommendation: dollars**, since that is the actual business objective, but report
   both because they diverge on large low-probability cases.

---

### M7 - Free-text intake (optional)

**Objective.** Turn a scam-report email or call note into a structured case, with a
human confirming.

**Build.** `adapters/freetext.py` filled in behind the M3 interface. A labelled
evaluation set. An abstention path. A confirmation step that cannot be bypassed.

**Acceptance.**
- [ ] Field-level extraction accuracy reported per field, not averaged into one number.
- [ ] Abstention rate reported separately; abstention is never counted as an error.
- [ ] No code path submits an extracted case without human confirmation. A test
      asserts this.
- [ ] Prompt-injection cases in the eval set - an email containing instructions must
      not alter behaviour.

**Excluded.** No fine-tuning. No auto-submission, at any confidence.

**Confirm before starting.** Which model, and does it run on free tier? If no free
option is workable, this milestone is the first cut - say so rather than adding cost.

---

### M8 - Operations console

**Objective.** The thing that gets demonstrated. A working queue an operator uses.

**Build.**
- `api/` - FastAPI over the case store.
- `console/` - queue view with live SLA clocks, case detail, disposition capture,
  evidence export, counterparty statistics.
- A seeded demo scenario that runs end to end in under 3 minutes.

**Acceptance.**
- [ ] A case can be taken from arrival to disposition entirely through the UI.
- [ ] SLA clocks show remaining banking time and visibly escalate.
- [ ] Triage ranking is visible, with the reason shown - an unexplained ranking will
      not survive an interview question.
- [ ] Evidence export downloads and verifies.
- [ ] The demo scenario is scripted and reproducible from a seed.
- [ ] p99 API latency under 300ms, asserted in a test.

**Excluded.** No authentication beyond a single demo operator. No multi-tenancy.

**Confirm before starting.** Server-rendered or a JS framework? **Recommendation:
server-rendered with HTMX** - fewer moving parts, no build step, deploys anywhere, and
nothing here needs a SPA.

---

### M9 - Network mode (optional)

**Objective.** Two Interlock instances exchanging cases machine to machine, proving the
upgrade path.

**Build.** Wire protocol over the M3 case model, reusing `schema/versioning.py`. Mutual
authentication. A two-instance integration test.

**Acceptance.**
- [ ] Two instances exchange a request and a disposition without human involvement.
- [ ] Version negotiation happens before parsing, per the existing design.
- [ ] Both instances' audit chains independently verify.
- [ ] **Single-instance mode is provably unaffected** - the full M3 to M8 test suite
      passes with networking disabled.

**Excluded.** No discovery service, no registry, no consortium governance. Two
configured peers.

---

### M10 - Deploy and harden

**Objective.** Running, publicly reachable, on free tier, with a URL that works during
an interview.

**Build.** Container, health checks, structured logging, backup of the audit chain, a
seeded public demo instance, and a README that gets a reader to a running copy in under
10 minutes.

**Acceptance.**
- [ ] Cold start under 10 seconds, or no cold start at all.
- [ ] Survives a restart with the audit chain intact and verifying.
- [ ] Demo instance reachable and seeded.
- [ ] A reader following the README reaches a running instance without asking anything.
- [ ] No secret in the repo. History scanned, not just the working tree.

**Confirm before starting.** Oracle Cloud free tier versus alternatives. Oracle gives
real always-on compute with no cold start but a heavier setup; the decision was left
open in v1.0 and should be settled before M10 rather than during it.

---

## 8. How the whole thing is measured

Four numbers, and only these get headline treatment:

1. **Dollars recovered per analyst-hour** versus arrival order, under a swept capacity
   constraint. The core claim.
2. **Share of cases answered within the rail's window.** Target 100%, because it is a
   process property rather than a modelling one - if it is below 100% the state machine
   has a bug.
3. **Median time from arrival to first action**, by channel. Where normalisation pays.
4. **Disposition completeness.** Must be 100% by construction. Any other value is a
   defect.

Explicitly *not* headline metrics: model AUC, fraud detection rate, any accuracy number
implied to transfer to production.

---

## 9. What this proves, and what it does not

**Proves.** That a cross-rail recall operations layer can be specified and built; that
the rail obligation asymmetry in section 2.1 is real and has engineering consequences;
that triage under a decay curve beats queue order; that a disposition guarantee can be
enforced structurally rather than by policy.

**Does not prove.** That any bank will buy it. That the recovery curve holds in the US -
no US dataset was found. That the model numbers mean anything outside this synthetic
data. That the network in M9 would ever gain participants.

Say all of this out loud in an interview before being asked. The candidate who names
their own limitations is more credible than the one who has to be caught.

---

## 10. Open questions

1. **Primary sources behind a wall.** FedNow Operating Procedures §15.2, the RTP
   Operating Rules, the Nacha Rules Book and the ISO 20022 external code sets are all
   gated or were untractable. Roughly four factual claims rest on secondary sources and
   are flagged UNVERIFIED throughout. Decide whether to chase them or ship with the
   flags visible. **Shipping with visible flags is defensible; silently guessing is
   not.**
2. **The RTP fraud carve-out** - that FRAD claims are exempt from the 10-banking-day
   window - rests on a single credible secondary source quoting rule VII.C.2. It is the
   sharpest line in the pitch and the least verified. Do not build it into the matrix
   as fact.
3. **Mastercard's US timeline for Recover.** If it lands, this becomes a
   differentiation question rather than a gap question. Worth a periodic check.
4. **Scope.** Ten milestones is a lot. Agree the cut line now, not at milestone eight.

---

## Appendix - sources for every claim above

Rails and rules:
- FedNow Operating Circular 8: https://www.frbservices.org/binaries/content/assets/crsocms/resources/rules-regulations/062425-operating-circular-8.pdf
- FedNow ISO 20022 Readiness Guide: https://explore.fednow.org/resources/readiness-guide-iso-20022.pdf
- FedNow fraud guidance: https://explore.fednow.org/resources/fraud-at-a-glance.pdf
- Nacha, April 2025 request-for-return response rule: https://www.nacha.org/rules/risk-management-topic-april-1-2025
- Nacha, October 2024 changes and False Pretenses: https://www.nacha.org/rules/risk-management-topics-october-1-2024
- Nacha fraud monitoring phase 1: https://www.nacha.org/rules/risk-management-topics-fraud-monitoring-phase-1
- Nacha fraud monitoring phase 2: https://www.nacha.org/rules/risk-management-topics-fraud-monitoring-phase-2
- Nacha return reason codes: https://www.nacha.org/rules/differentiating-unauthorized-return-reasons
- Nacha Risk Management Portal, letters of indemnity: https://www.nacha.org/news/now-nachas-risk-management-portal-secure-exchange-standardized-letters-indemnity
- TCH RTP technical documentation: https://www.theclearinghouse.org/payment-systems/rtp/technical-documentation
- RTP rule VII.C.2 [SECONDARY, UNVERIFIED]: https://www.cuanswers.com/wp-content/uploads/RTP-Participant-Self-Audit-Guidebook.pdf
- Fedwire ISO 20022 FAQ: https://www.frbservices.org/resources/financial-services/wires/faq/iso-20022/format
- Fed Exception Resolution Service: https://www.frbservices.org/financial-services/ach/exception-resolution.html

Market and regulatory:
- Fed/FDIC/OCC payments fraud RFI: https://www.federalreserve.gov/newsevents/pressreleases/files/bcreg20250616a1.pdf
- TCH comment letter on the RFI: https://www.fdic.gov/federal-register-publications/clearing-house-stephen-krebs-rin-3064-za49.pdf
- Payments Dive on interbank communication: https://www.paymentsdive.com/news/banks-credit-unions-communication-payments-fraud/748226/
- ASC X9 Payment Fraud Forum: https://www.pymnts.com/cybersecurity/fraud-prevention/2026/new-forum-wants-banks-speaking-the-same-fraud-language/
- CFPB v. Early Warning Services, dismissed: https://www.consumerfinance.gov/enforcement/actions/early-warning-services-llc-bank-of-america-na-jpmorgan-chase-bank-na-wells-fargo-bank-na/
- CFPB interpretive rule withdrawal: https://www.federalregister.gov/documents/2025/05/15/2025-08646/electronic-fund-transfers-through-accounts-established-primarily-for-personal-family-or-household

Competitive:
- Mastercard A2A Protect Recover (UK): https://www.mastercard.com/news/europe/en-uk/newsroom/press-releases/en-gb/2025/prevent-protect-and-recover-mastercard-strengthens-trust-in-account-to-account-payments/
- Salv Bridge: https://salv.com/product/salv-bridge/
- Quavo rail coverage: https://www.quavo.com/qfd/
- Feedzai IQ Score, June 2026: https://www.prnewswire.com/news-releases/feedzai-opens-9-trillion-fraud-intelligence-network-to-every-bank-delivering-4x-improvement-in-fraud-detection-from-day-one-302794333.html
- Visa completes Featurespace acquisition: https://investor.visa.com/news/news-details/2024/Visa-Completes-Acquisition-of-Featurespace/default.aspx

Data:
- IC3 2025 Annual Report: https://www.ic3.gov/AnnualReport/Reports/2025_IC3Report.pdf
- FedNow volume and value: https://www.frbservices.org/resources/financial-services/fednow/volume-value-stats
- Richmond Fed on FedNow participation: https://www.richmondfed.org/publications/research/economic_brief/2026/eb_26-28
- TCH RTP volumes: https://www.theclearinghouse.org/payment-systems/rtp
- RUSI, Following the Fraud - the mule decay curve: https://static.rusi.org/following-the-fraud-the-role-of-money-mules.pdf
- UK Finance Annual Fraud Report 2026: https://www.ukfinance.org.uk/news-and-insight/press-release/fraud-report-2026-press-release
- PSR reimbursement dashboard: https://www.psr.org.uk/information-for-consumers/app-scams-reimbursement-dashboard/
