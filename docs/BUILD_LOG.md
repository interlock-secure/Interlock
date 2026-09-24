# Build log

What each milestone produced, and - more usefully - what its acceptance
criteria caught. Every defect below was found by a test rather than by
reading, and the reasoning is the most transferable part of the work.

| Milestone | State | Tests |
|---|---|---|
| M0 toolchain | complete | packaging, CI, lint and coverage gates |
| M1 wire schema | complete | typed models, hash-chained audit, privacy allowlist, version negotiation |
| M2 synthetic testbed | complete | four institutions, per-account behaviour, continuous mule sophistication |
| M3 case model and adapters | complete | canonical case, rail matrix with provenance, four channels |
| M4 SLA and ledger | complete | Federal Reserve calendar, state machine, disposition ledger, evidence export |
| M5 recall case generator | complete | decay calibration, heterogeneous counterparties, temporal splits |
| M6 recoverability model | complete | ranking under capacity, three baselines, calibration |
| M7 extraction evaluation | complete | per-field accuracy, abstention, injection resistance |
| M8 operations console | complete | queue, case detail, disposition capture, evidence, demo seed |
| M9 network mode | complete | signed peer exchange, single-instance operation unaffected |
| M10 deploy | complete | container, health check, README, support contact |

**397 tests, 92% coverage** at the end of M10, before the reviews below. Lint and format clean.

---

## Defects the acceptance criteria caught

Listed because the interesting part of a build is not what worked.

### M2 — four rounds of the same lesson

| Defect | How it surfaced | Fix |
|---|---|---|
| A declared feature carrying no information | `inbound_per_month` was set per archetype but receivers were drawn uniformly, so every archetype had the same median first-time inbound count | Weighted receiver selection |
| Precision reached 1.000 | After that fix, mules were the only accounts both young and busy | A population of legitimate young, busy accounts |
| An 18-minute timing fingerprint | Archetypes shared constants; a single threshold scored F1 0.916 | Per-account draws, plus businesses on automatic treasury sweeps. F1 fell to 0.661 |
| Mule population mis-sized | A fixed share produced ~4 fraud payments per mule | Derive the count from fraud volume |

**The rule this established:** when synthetic data produces a suspiciously
good result, the data is wrong, not the model. Fix it by adding legitimate
lookalikes, never by weakening the detector.

### M3 — a hardcoded deadline

The AST guard against rail rules written outside the capability matrix found a
real offender on its first run: `DEFAULT_RECALL_SLA = timedelta(hours=24)` in
`recall.py`. Now a function reading the matrix.

A first version of that guard grepped for phrases and drowned in false
positives from docstring prose and the generator's sweep delays. The narrower
AST form was kept precisely because it had caught something real.

### M4 — house policy presented as a rail rule

`require_verified_window()` checked provenance confidence but not
`window_is_house_policy`, so FedNow's 24-hour house policy was returned as a
verified rail rule and labelled binding. That is exactly the confusion the
matrix exists to prevent: presenting our own preference to a counterparty as
their obligation. It now refuses.

### M5 — two defects, both shape-of-the-data

**The decay curve.** A single exponential, with a docstring arguing a second
phase was "not justified by three observations". Wrong: no single exponential
passes within tolerance of all three RUSI anchors. A two-phase fit lands at
RMSE 0.0003, and the split it finds is not an artifact — 57% of value on a
21-minute time constant and 43% on a 16-hour one are the crude and patient
operators the M2 generator already models.

**The outcome label.** It said every case recovered. With a residual floor
some funds are nearly always present, and the label conflated *funds remain*
with *recovery succeeded*. But returning is discretionary on three of four
rails, which is the product's whole premise, so the institution now has to
decide. Recovery needs funds present, a willing counterparty, and a material
share returned.

### M6 — two dishonest numbers

The first report quoted uplift over arrival order as a multiple, reaching
212x. True, and worthless: arrival order recovers so little at low capacity
that the denominator is noise. Replaced with absolute dollars and a margin
against largest-amount-first, which is the competitor that has to be beaten.

And the gradient booster does not beat the logistic regression — better Brier,
worse AUC, both by margins too small to call. The report says so in a line of
its own.

### M7 — a hallucinated reason

"No fraud involved, just a posting error on our side" was classified as a
scam, because the word *fraud* appears in it. Inventing a reason from a
message explicitly denying one starts a formal claim against a customer's
account on the strength of a word. Negation detection added, bounded to one
clause so a denial followed by a real claim still extracts.

### M8 — a UK research paper cited as the Federal Reserve's rule

FedNow and Fedwire set no window, so their window provenance points at the
RUSI mule-account paper behind our house policy — and the rails page was
presenting that as "the FedNow source". A reader would reasonably conclude the
Fed's rules come from a British PDF. `RailProfile` now carries
`rail_authority_url` separately.

---

## Honest limitations

- **The container has never been built.** No Docker daemon was available. Its
  properties are asserted by tests; the first `docker compose up` is the real
  test.
- **No model call is made** in the free-text extractor. The harness is what a
  model-backed one would be measured against.
- **The decay curve is UK-derived.** No US time-to-dissipation dataset was
  found to exist. Labelled as an assumption everywhere it is used.
- **Four rail rules are unverified**, because the FedNow Operating Procedures,
  the RTP Operating Rules and the Nacha Rules Book are gated, robots-disallowed
  or paywalled. None is permitted to drive a deadline.
- **All data is synthetic.** No figure here transfers to production.

---

## Independent review, and what it broke

After M10 an agent that had not seen the build was asked to attack the
project's own claims. It found that several of the strongest ones were false,
and it was right. Every finding below was reproduced, fixed and re-tested.

| Finding | Severity | What was actually true | Fix |
|---|---|---|---|
| DISPOSED reachable without a disposition | Critical | The guarantee lived only in `apply()`. Direct construction, `dataclasses.replace` and rehydrating a corrupt database row all produced the object the product claims cannot exist, and the export then printed `closed_without_disposition: 1` beside the words "zero by construction" | `__post_init__` invariant on every construction, plus a SQL CHECK so the row cannot be written |
| Audit chain passed four tampers | Critical | Tail truncation, a falsified summary, a deleted chain and a forward-recomputed edit all verified clean | Declared length checked; headline figures re-derived from the chain; prose corrected - the chain is unkeyed and cannot stop an institution editing its own history, and now says so |
| Acknowledged breaches counted as met | High | A case closed with an operator formally recording "we missed the window" counted as answered in time, because classification used timestamps alone | `SLA_EXPIRED_ACKNOWLEDGED` forces breached unconditionally |
| Leakage guard was a naming convention | High | `feature_matrix` never consulted `FORBIDDEN_FEATURES`. Injecting `share_remaining` passed all 25 triage tests | Runtime refusal in `feature_matrix`; the tautological test replaced with ones that inject a leak and assert failure. The reviewer's exact probe now fails 11 tests |
| Model fed the generator's own parameters | High | Counterparty features were the true latent values the label was drawn from. No production system has those | Estimated from closed cases only, shrunk toward a prior. AUC fell from 0.708 to 0.679 - that is the honest figure |
| Deadlines drifted on every read | Medium | `open_case` promised a fixed deadline; `_hydrate` recomputed it, so a rules change rewrote historical breach findings | Stored deadline read back from the row |
| Deadlines not in UTC | Medium | Docstring said UTC; code used the caller's offset, a five-hour swing on a Nacha window | Computed in UTC regardless of input |
| `except Exception` around XML parsing | Medium | Turned MemoryError and RecursionError into "malformed message" | Named exception types only |

**The pattern the reviewer named is worth keeping:** the docstrings wrote
cheques the code did not cash, in exactly the places the project marketed
itself, and the confident prose made each gap read as overclaiming rather than
oversight. High coverage on tests built to confirm the design rather than
attack it was cited as evidence, which made it worse than having fewer, honest
tests.

What held up: the banking calendar, the rail provenance model and its visible
house-policy fallback, the AST guard against hardcoded deadlines, constant-time
signature comparison, defusedxml, integer cents that refuse to round, and fully
parameterised SQL.

**402 tests, 92% coverage** after the fixes.

---

## Second review: the fixes, attacked

A second agent that had seen neither the build nor the first review was asked
to check whether the fixes held. Its summary: "The fixes block the exact routes
the previous review reported, but the verifier still doesn't check the
headline compliance figures." It was right about that and about the rest.

| Finding | Severity | What was actually true | Fix |
|---|---|---|---|
| Compliance split not bound to the chain | Critical | The verifier checked answered + breached against the disposition count, which nobody would falsify, and never the split, which is what an institution would falsify. Turning breaches into 100% compliance verified clean | Every per-rail figure and the compliance rate are re-derived from chain payloads. Each disposal entry's own recorded breach verdict is also checked against its own times |
| Verifier trusted the export's description of itself | High | A zero-length chain declared as zero skipped every check; missing summary blocks were skipped rather than reported | Declared length must be an integer and match; missing blocks are problems; rails on the chain but absent from the summary are problems; malformed links are reported rather than raised |
| Two deadline rules | High | The clock treated `due_at` itself as breached, the summary as answered. A disposal at exactly the deadline carried `breached_at_transition: True` and counted as compliant | One function, `answered_in_time`, used by clock, summary and verifier |
| Filtered case list broke verification | Medium | `build_export` given a subset of cases produced an export that failed its own check | Refuses when cases and chain disagree |
| Deadline half-stored | Medium | Only `due_at` and authority were persisted; window and operator-facing reason were recomputed, so a rules change left a case due on the old date explaining itself with the new rule | Full deadline stored as JSON, checked against the `due_at` column on read |
| Storage CHECKs bypassable | Medium | Only on new files (`CREATE TABLE IF NOT EXISTS` keeps an old table); `disposition = ''` satisfied `IS NOT NULL`; a disposal needed no actor or time | Enumerated states and dispositions; actor and time required; schema version in `PRAGMA user_version`; old files rebuilt on open, and one with a violating row refused untouched. A `length(trim(NULL)) > 0` CHECK evaluates to NULL and passes - caught by the new test, fixed with an explicit `IS NOT NULL` |
| Leakage guard was a text search | High | It looked for `.share_remaining` in the function source. `getattr`, a helper, or string concatenation all passed. It also cited an ablation test that did not exist | Feature code gets a read-only view that raises on any post-outcome field or generator parameter. Tests inject leaks by `getattr`, via a helper, and by latent parameter. The ablation reference is removed |
| Fit rows saw their own label | High | Counterparty history was one table built from all fitting rows, so each row's feature included its own outcome | `history_as_of`: each row sees only cases that closed before it arrived. A test flips a row's label and asserts its own features do not move. Booster AUC 0.679 to 0.665 |
| Verdicts overstated the model | Medium | "Clears noise" printed beside a worst seed of -$12,077; the report called the linear model defensible while the harness ranked with the booster | Verdict is now "never loses" only when ahead on average and behind on no seed. Ranking uses the logistic regression as a fixed rule. It never lost a seed against largest-first at capacity 5 to 60 and lost one at 100, and the report says so |
| `RecallCase.state` never updated | Low | A second copy of state on the wire model read `received` forever | Removed. Legacy JSON with `"state": "received"` still loads; any other value is refused |

Current model figures, five seeds: logistic regression Brier 0.0856, AUC
0.698; gradient boosting Brier 0.0858, AUC 0.665. The booster does not earn its
complexity on this data.

Not fixed, and stated: the chain is still unkeyed, so an institution that
recomputes it, or drops its newest entries and edits the summary to match,
passes verification. `pickle` bypasses `__post_init__`. The container has
never been built.


---

## Third review

A third fresh reviewer confirmed the second round held on compliance figures,
leakage via `vars`/`asdict`/`pickle`/`copy`, the as-of history, and triage
numbers (fresh run byte-identical to the committed report). It found and we fixed:

| Finding | Fix |
|---|---|
| `deadline_is_binding` and source fields never verified - a missed Nacha obligation could be relabelled house policy | Binding re-derived from opening entries; source, confidence and basis checked against the rail matrix |
| Disposal dated before the case arrived verified as on time | `apply()` refuses a step before arrival or the previous step; verifier reports it |
| Five malformed inputs still raised | Any structural failure is returned as a problem, never a traceback |
| Refused legacy file was modified (WAL header) | Migration runs before WAL is enabled; byte-identical test. Missing columns give a clear refusal |
| `__replace__` and pickling hooks unwrapped the leakage view | Those dunders raise `LeakageError` |
| Boolean counts accepted as integers | Strict `int` type check |

474 tests, 92.9% coverage, ruff clean.

---

## The AI layer

Added after the third review, at the product owner's request for AI at the
centre rather than the edge. Claude (via the Anthropic SDK's structured
outputs) backs three features: intake extraction, outcome suggestions and
reply drafting. Design rule: the model proposes, deterministic code checks,
a named person decides, and the audit trail records whether they followed it.

| Decision | Why |
|---|---|
| Verbatim-quote check on every extracted field | Structured output stops malformed answers, not wrong ones. A quote that is not in the message is an invented quote |
| Institutions never taken from text | A sender can claim to be any bank; identity comes from the channel |
| Suggestions limited to legal actions and supplied facts | A citation to an unsupplied fact means the model reasoned from something it made up |
| `ai_suggested` and `followed_ai` on the chain | Adoption is measured from evidence, not a side log; added only when present so older entries keep their digests |
| htmx served locally | A browser test found the console's buttons dead when the CDN was unreachable. Same file, hash verified |
| Blank rail and reason force a choice | A browser test showed a blank rail silently pre-selecting FedNow |

The labelled set grew from 8 to 23 messages (6 hostile). On it the
rules-based baseline made up one amount (a keying-error message quoting two
figures), which the original 8 never exercised.

**Not yet measured:** the AI extractor's accuracy. No API key was available
where this was built, so every AI test uses scripted model outputs. The
evaluation command exists and reports "NOT RUN" rather than a number.

514 tests, 92.9% coverage, ruff clean.
