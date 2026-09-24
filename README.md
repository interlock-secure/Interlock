# Interlock

**A cross-rail recall and return operations system for US banks.**

Every US payment rail has a way to ask for money back. Not one has both a
machine-readable format for the answer and an enforceable obligation to give
one.

| Rail | Disposition format | Response obligation | Window |
|---|---|---|---|
| **ACH** | **none** — Nacha states the method is flexible, portal or phone | **mandatory** | **10 banking days, even when refusing** |
| RTP | camt.029 | mandatory | 10 banking days, *fraud reportedly exempt* |
| FedNow | camt.029 | advisory — Operating Circular 8 says "should" | none we could establish |
| Fedwire | camt.029 | advisory | none stated |

Read the first row against the rest. **ACH has the obligation and no format;
the instant rails have the format and no enforceable obligation.** A bank
working all four runs four incompatible processes for one business event:
*my customer was tricked, please send the money back*.

Interlock is the operations layer over that gap. It normalises a return-of-funds
request from any channel into one case with one clock, ranks the queue by
recoverable value rather than arrival order, and makes it structurally
impossible to close a case without a recorded disposition.

---

## What this repository is meant to show

This project was built end to end — problem framing, specification, build,
review, and an AI layer — as a single artifact that speaks to three different
kinds of reviewer at once, because the work of shipping AI products actually
requires all three:

- **As product work:** the opening table is the whole thesis. The regulatory
  gap it names (ACH has the obligation and no format, instant rails have the
  format and no obligation) is the reason this product needs to exist, not a
  feature list. The [**What this proves, and what it does not**](#what-this-proves-and-what-it-does-not)
  section is the part most decks skip — a plain statement of which claims are
  load-bearing, which are unverified, and which numbers come from synthetic
  data rather than a real bank. Product judgment shows up as much in that
  section as in the build.
- **As program/technical-program work:** the [**three rules**](#the-three-rules)
  are non-negotiable requirements carried through the whole stack — schema,
  state machine, storage, and a static check that fails the build if a rail
  deadline is ever written outside its one source of truth. `docs/BUILD_PLAN.md`
  and `docs/BUILD_LOG.md` are the actual planning and decision record, not
  after-the-fact narrative: they show milestones, a framing that got replaced
  mid-project and why, and design trade-offs (AUC given up for leakage
  correctness, for example) made and logged in the open.
- **As AI/ML engineering work:** the [**AI layer**](#the-ai-layer) is built
  on the same discipline as the rest of the system — every model output is
  proposed, checked in code (not just prompted for), and logged, never
  auto-applied. Four rounds of independent review found and closed real gaps
  (wrong-amount extraction, prompt injection, forged adoption metrics), and
  every fix has a regression test. The [**triage model**](#verifying-it)
  section is a small, complete example of an ML evaluation done honestly:
  a baseline it has to beat, a metric it could lose on, and the seed where it
  came closest, printed rather than hidden.

---

## Running it

Two commands, from a cold clone, in under ten minutes.

```bash
uv sync --all-extras
uv run uvicorn interlock.api.app:app --reload
```

Open <http://127.0.0.1:8000>. The instance seeds its own demo queue
deterministically, so what you see is the same every time.

Or in a container:

```bash
docker compose up --build
```

> **Not yet built in anger.** The `Dockerfile` has been reviewed and its
> properties are asserted by tests — non-root user, health check wired to
> `/healthz`, database on a writable volume, virtualenv excluded from the
> build context — but no Docker daemon was available in the environment it was
> written in, so the image has never actually been built. Treat the first
> `docker compose up` as the real test.

### Configuration

Every setting is an environment variable, and every default is in
[`src/interlock/config.py`](src/interlock/config.py).

| Variable | Default | What it does |
|---|---|---|
| `INTERLOCK_SUPPORT_EMAIL` | `support@interlock-secure.com` | The support contact shown in the console footer, the API description and the health check |
| `INTERLOCK_INSTITUTION_NAME` | `Harbor National` | Which institution this instance runs as |
| `INTERLOCK_DATABASE` | `data/interlock.db` | SQLite path; `:memory:` for ephemeral |
| `INTERLOCK_SEED_DEMO` | `1` | Seed the demo queue on an empty database |
| `INTERLOCK_DEMO_SEED` | `20260919` | Makes the demo reproducible |

**Support contact:** <support@interlock-secure.com>

That address is defined once, in `config.py`, and a test walks the source tree
to prove it is not hardcoded anywhere else. The default matches the GitHub
organisation rather than a personal mailbox on purpose — an address published
in a public repository is scraped within days and cannot be unpublished, so
pointing it somewhere else should be a deliberate choice.

---

## What is here

```
src/interlock/
  schema/      the contract - canonical case, rail capability matrix, audit chain
  adapters/    camt.056, camt.029, ACH R06 and return records, free-text intake
  sla/         Federal Reserve banking calendar, per-rail clocks
  recall/      state machine, disposition ledger, evidence export, storage
  triage/      recoverability model, ranking policy, evaluation harness
  generator/   synthetic payments, recall cases, decay model, counterparties
  ai/          Claude-backed intake, suggestions, reply drafts, evaluation
  api/         the console
  network/     optional peer-to-peer mode
```

### The three rules

1. **No recall case closes without a recorded disposition.** Window expiry is
   itself a disposition requiring acknowledgement, not an absence of one.
   Enforced in three places, because an independent review found the first
   one alone could be bypassed: the state machine offers no transition to a
   closed state without a disposition; the case object refuses construction
   in that shape through its constructor, `dataclasses.replace` and
   rehydration from storage (not through `pickle`, which skips `__init__`);
   and the database carries CHECK constraints - enumerated states and
   dispositions, a named actor and a time on every disposal - so a violating
   row cannot be written. Databases created before those constraints are
   rebuilt on open, and one holding a violating row is refused rather than
   repaired.
2. **Never return "low risk" when we mean "we don't know."**
3. **Rail rules live in one place and carry their provenance.** Every deadline
   comes from `schema/rails.py` with a source URL and a confidence level. Code
   depending on an unverified rule raises rather than guessing, and a static
   check enforces that no deadline is written anywhere else.

---

## The AI layer

Interlock uses Claude (Anthropic's model) as an assistant to the operator, in
three places. In each one the model proposes and a named person decides.

| Feature | What the AI does | What stops it going wrong |
|---|---|---|
| **AI intake** (`/intake`) | Reads a recall request that arrived as an email or call note and proposes a case | Every value must quote the message word for word or it is dropped; amounts are checked digit by digit against their quote; bank identities are never taken from the text; nothing is filed until an operator confirms it |
| **AI suggestion** (case page) | Suggests the next outcome for a case, with its reasons | Must be a legal action for the case, may cite only facts it was given, and cannot suggest giving up before the deadline. Rejected answers are shown as rejected, never replaced by a default |
| **AI reply draft** (closed case) | Writes the note back to the requesting bank | Must state the recorded outcome, case id and exact amount; may contain no number that is not in the case; is never sent automatically |

Whether a person followed or overrode each suggestion is written to the audit
trail, and `/ai` reports the follow rate and the most common overrides from
that trail.

**Switching it on:** set `ANTHROPIC_API_KEY` (and optionally
`INTERLOCK_AI_MODEL`, default `claude-sonnet-5`). Without a key every AI panel
says "not configured" and the intake page falls back to the rules-based
extractor, labelled as such.

**Measuring it:** `uv run python -m interlock.ai.evaluate` scores the AI intake
agent and the rules-based baseline on a labelled set of 23 messages, 6 of them
prompt-injection attempts, and writes `docs/eval/ai_extraction_report.txt`.
The committed report contains the baseline only, because no API key was
available where this was built: **the AI's own extraction accuracy has not yet
been measured**, and no figure for it is claimed anywhere.

The queue order on the console comes from the published decay curve, not from
the M6 model or the language model.

---

## Verifying it

```bash
uv run pytest                    # 514 tests, 93% coverage, gate at 70%
uv run ruff check . && uv run ruff format --check .
```

Some tests worth knowing about:

- **The disposition guarantee** is walked exhaustively over all 66,430
  transition sequences up to length five, plus 2,000 hypothesis-generated ones
  including illegal moves - and separately checked against direct
  construction, `dataclasses.replace` and a corrupted database row, the three
  bypasses a review found that the transition walk could not see.
- **The banking calendar** is checked against the Federal Reserve's published
  dates for 2026 and 2027, including the asymmetry most implementations get
  wrong: a holiday on Saturday leaves Reserve *Banks* open the preceding
  Friday, while one on Sunday closes everything the following Monday.
- **Tamper detection** catches corruption, removed or reordered entries,
  altered payloads, and every summary figure that disagrees with the events
  beneath it: the verifier re-derives each rail's answered, breached and open
  counts and the compliance rate from the chain itself, using the same
  deadline rule the summary was built with. **It does not catch an
  institution that edits its own history and recomputes the chain, or drops
  the newest entries and edits the summary to match** - the chain is unkeyed,
  and no self-contained unkeyed document can. Closing that needs an external
  anchor, which is not built. The export says so in its own disclaimer.
- **Leakage** is refused at runtime: feature code is handed a read-only view
  of each case that raises on any post-outcome field or generator parameter,
  including reads through `getattr` or a helper. Counterparty features for
  each row are estimated only from cases that had closed before that row
  arrived, so no row sees its own label. The AUC cost of each of those fixes
  is recorded in `docs/BUILD_LOG.md`.
- **Triage** ranks with the logistic regression, which matched or beat the
  gradient booster on both Brier and AUC. Against largest-amount-first it
  never lost a seed at capacities 5 to 60 and lost one seed at 100; the report
  prints the worst seed next to every margin (`docs/eval/triage_report.txt`).

Evaluation reports are regenerated with:

```bash
uv run python -c "from interlock.triage.harness import evaluate, render_report; print(render_report(evaluate()))"
```

---

## What this proves, and what it does not

**Proves.** That a cross-rail recall operations layer can be specified and
built; that the rail obligation asymmetry is real and has engineering
consequences; that triage under a decay curve beats queue order, and on this synthetic data
beats largest-amount-first at scarce capacity; that a
disposition guarantee can be enforced structurally rather than by policy.

**Does not prove.** That any bank will buy it. That the recovery curve holds in
the US — no US dataset was found, and the curve here is calibrated from UK
mule-account data and labelled as such everywhere it is used. That the model
numbers mean anything outside this synthetic data. That the optional network
mode would ever gain participants.

Four rail rules could not be verified against a primary source, because the
FedNow Operating Procedures, the RTP Operating Rules and the Nacha Rules Book
are variously gated, robots-disallowed or paywalled. They are marked
`UNVERIFIED` in the matrix, surfaced on the console, and are not permitted to
drive any deadline.

**All data in this repository is synthetic.** No real institution's behaviour
appears anywhere in it, and no figure it produces transfers to production.

---

## Specification

`docs/PRD_v2_0.docx` is the specification. `docs/BUILD_PLAN.md` explains the
strategy and why v1.1's signal-network framing was replaced.
`docs/protocol/rails.md` is generated from the code, and a test fails if the
two drift apart.

## Licence

MIT.
