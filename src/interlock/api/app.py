"""The operations console and its API.

What this surface is for
------------------------
One analyst, one queue, one clock per case. Everything on screen answers a
question somebody actually asks:

    Which case should I work next?        -> the ranking, with its reason
    How long have I got?                  -> the clock, with its authority
    Is that a real deadline?              -> solid badge or dashed
    What did we tell them last time?      -> the timeline
    Can I prove we answered in time?      -> the evidence export

Server-rendered HTML with HTMX for the interactive parts. No build step, no
bundle, no framework to keep current - which matters for something that has to
still run from a cold repository in a year's time.

Ranking, and why it is not the model at request time
----------------------------------------------------
The console ranks by expected recoverable value, and computes the probability
with the heuristic in :func:`recoverability_estimate` rather than by loading
the M6 model. That is a deliberate trade: loading scikit-learn and a fitted
estimator into a free-tier web process costs startup time and memory for a
demo whose ordering the heuristic reproduces closely, and the honest
evaluation of the model lives in the M6 report where it can be scrutinised
properly. The console says which it is using rather than implying a model is
scoring live.
"""

from __future__ import annotations

import secrets
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from interlock.adapters.directory import suggest_requesting_bank
from interlock.adapters.documents import DocumentError, text_from_upload
from interlock.adapters.extraction_eval import evaluate_extractor
from interlock.adapters.freetext import KeywordExtractor
from interlock.adapters.responses import formal_reply
from interlock.ai.client import default_model
from interlock.ai.drafting import draft_reply
from interlock.ai.evaluate import EXTENDED_CORPUS
from interlock.ai.intake import ClaudeExtractor
from interlock.ai.metrics import adoption
from interlock.ai.recommend import recommend
from interlock.api import demo
from interlock.config import Settings, settings
from interlock.generator.recovery import expected_share_remaining
from interlock.recall.evidence import build_export, verify_export
from interlock.recall.state import DISPOSITION_OF, CaseFile, IllegalTransitionError, Transition
from interlock.recall.store import SqliteCaseRepository
from interlock.schema.case import RecallReason, cents_from_decimal
from interlock.schema.common import Rail
from interlock.schema.rails import profile_for, unverified_rules
from interlock.sla.clock import is_at_risk

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def recoverability_estimate(case_file: CaseFile, *, now: datetime) -> float:
    """Probability that funds are still recoverable, for ordering the queue.

    The published decay curve, adjusted for how the request arrived. Not the
    M6 model - see the module docstring for why - and the console labels it as
    an estimate rather than a prediction so nobody reads a modelled number off
    a screen that is not showing one.
    """
    minutes = case_file.case.minutes_since_settlement(at=now)
    if minutes is None:
        # No settlement time means the case cannot be ranked. Mid-scale rather
        # than zero, so it sits in the middle of the queue and gets looked at
        # instead of sinking out of sight.
        return 0.35

    share = expected_share_remaining(minutes)

    # A request that arrived as a rail message reached us faster and carries
    # the counterparty's own reference, so it is marginally likelier to be
    # actionable than one retyped from an email.
    if case_file.case.channel.is_structured:
        share *= 1.05

    return min(1.0, share)


def rank(cases: list[CaseFile], *, now: datetime) -> list[dict[str, Any]]:
    """Order the queue by expected recoverable value."""
    rows = []
    for case_file in cases:
        probability = recoverability_estimate(case_file, now=now)
        rows.append(
            {
                "file": case_file,
                "probability": probability,
                "expected_cents": probability * case_file.case.amount_cents,
            }
        )
    rows.sort(key=lambda r: (-r["expected_cents"], r["file"].case.received_at))
    for position, row in enumerate(rows, start=1):
        row["rank"] = position
    return rows


def _elapsed_label(case_file: CaseFile, now: datetime) -> str:
    minutes = case_file.case.minutes_since_settlement(at=now)
    if minutes is None:
        return "unknown"
    if minutes < 60:
        return f"{int(minutes)} min"
    if minutes < 1440:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} d"


def _remaining_label(case_file: CaseFile, now: datetime) -> str:
    seconds = case_file.deadline.remaining(at=now)
    if seconds <= 0:
        return f"overdue {abs(seconds) / 3600:.0f} h"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _view(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One queue row, flattened for the template."""
    case_file: CaseFile = row["file"]
    return {
        "rank": row.get("rank"),
        "case_id": case_file.case_id,
        "rail": case_file.case.rail.value,
        "channel": case_file.case.channel.value,
        "counterparty": case_file.case.requesting_institution_id,
        "amount": f"${case_file.case.amount_cents / 100:,.2f}",
        "reason": case_file.case.reason.value.replace("_", " "),
        "elapsed": _elapsed_label(case_file, now),
        "probability": f"{row['probability']:.0%}",
        "probability_value": row["probability"],
        "expected": f"${row['expected_cents'] / 100:,.0f}",
        "remaining": _remaining_label(case_file, now),
        "binding": case_file.deadline.is_binding,
        "authority": ("rail rule" if case_file.deadline.is_binding else "house policy"),
        "at_risk": is_at_risk(case_file.deadline, at=now),
        "breached": case_file.is_breached(at=now),
        "state": case_file.state.value,
    }


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


SAMPLE_REQUEST = (
    "From: fraud.ops@northbay-cu.example\n"
    "Subject: Urgent - request for return of funds\n\n"
    "One of our members was scammed on 17 September. The transfer of $4,820.00, "
    "trace number 091000019887766, was sent after a caller impersonated our fraud team. "
    "Please return the funds or confirm what you are able to recover.\n\n"
    "Fraud Operations, Northbay Credit Union"
)


def _log_intake_checks(repo: SqliteCaseRepository, draft) -> None:
    """One row per model answer the intake checks dropped, or one 'ok'."""
    if any(n.startswith("AI intake unavailable") for n in draft.notes):
        repo.record_ai_check("intake", "unavailable", draft.notes[0])
        return
    dropped = [n for n in draft.notes if "model answer dropped" in n]
    for note in dropped:
        repo.record_ai_check("intake", "dropped", note)
    if not dropped:
        repo.record_ai_check("intake", "ok")


def _check_table(counts: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    rows = []
    for feature in ("intake", "suggestion", "draft"):
        row = {"feature": feature}
        for outcome in ("ok", "dropped", "rejected", "unavailable"):
            row[outcome] = counts.get((feature, outcome), 0)
        rows.append(row)
    return rows


_NO_MODEL = object()


def create_app(config: Settings | None = None, *, ai_model: Any = _NO_MODEL) -> FastAPI:
    """Build the application.

    A factory rather than a module-level app so tests get an isolated
    in-memory instance instead of sharing one database between them.
    """
    config = config or settings()
    model = default_model() if ai_model is _NO_MODEL else ai_model

    ready = threading.Lock()

    def ensure_ready(app: FastAPI) -> SqliteCaseRepository:
        """Open storage and seed the demo once, on startup or first request.

        Serverless hosts such as Vercel do not always run the ASGI lifespan
        hook, so every request path also calls this. The lock stops two
        concurrent first requests from seeding twice.
        """
        with ready:
            if getattr(app.state, "repo", None) is None:
                repo = SqliteCaseRepository(config.database_path)
                if config.seed_demo_data:
                    demo.seed(repo, config)
                app.state.config = config
                app.state.repo = repo
        return app.state.repo

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repo = ensure_ready(app)
        yield
        repo.close()
        app.state.repo = None

    app = FastAPI(
        title="Interlock",
        description=(f"Cross-rail recall and return operations. Support: {config.support_email}"),
        version="2.0.0",
        lifespan=lifespan,
    )

    app.state.repo = None
    app.state.config = config
    app.state.ai_model = model
    app.state.drafts = {}
    app.state.shown_suggestions = {}
    """case id -> the AI suggestion last shown for it. The transition form may
    only claim a suggestion that is recorded here, so adoption figures cannot
    be fed by a forged form field."""

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    def repo_of(request: Request) -> SqliteCaseRepository:
        return ensure_ready(request.app)

    def base_context(request: Request) -> dict[str, Any]:
        ensure_ready(request.app)
        return {
            "support_email": request.app.state.config.support_email,
            "institution": request.app.state.config.institution_name,
        }

    # -- health --------------------------------------------------------

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        """Liveness plus the one thing worth checking: does the chain verify?"""
        entries = repo_of(request).audit_entries()
        from interlock.recall.ledger import rebuild_chain

        problems = rebuild_chain(entries)
        return JSONResponse(
            {
                "status": "ok" if not problems else "chain_broken",
                "chain_length": len(entries),
                "chain_problems": problems,
                "support": request.app.state.config.support_email,
            },
            status_code=200 if not problems else 500,
        )

    # -- queue ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def queue(request: Request) -> HTMLResponse:
        now = datetime.now(UTC)
        rows = rank(repo_of(request).open_cases(), now=now)
        views = [_view(r, now) for r in rows]

        return TEMPLATES.TemplateResponse(
            request=request,
            name="queue.html",
            context={
                **base_context(request),
                "rows": views,
                "breaching": sum(1 for v in views if v["at_risk"] or v["breached"]),
                "total_expected": f"${sum(r['expected_cents'] for r in rows) / 100:,.0f}",
                "unverified": [
                    {"rail": rail.value, "note": p.note or p.claim}
                    for rail, p in unverified_rules()
                ],
            },
        )

    # -- case ----------------------------------------------------------

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    async def case_detail(request: Request, case_id: str) -> HTMLResponse:
        case_file = repo_of(request).get(case_id)
        if case_file is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")

        now = datetime.now(UTC)
        profile = profile_for(case_file.case.rail)

        return TEMPLATES.TemplateResponse(
            request=request,
            name="case.html",
            context={
                **base_context(request),
                "c": _view(
                    {
                        "file": case_file,
                        "probability": recoverability_estimate(case_file, now=now),
                        "expected_cents": 0,
                    },
                    now,
                ),
                "case_file": case_file,
                "deadline_reason": case_file.deadline.reason,
                "provenance": case_file.deadline.provenance,
                "rail_notes": profile.notes,
                "history": [
                    {
                        "transition": s.transition.value.replace("_", " "),
                        "actor": s.actor,
                        "at": s.at.strftime("%d %b %H:%M"),
                        "reason": s.reason,
                        "ai": s.ai_suggested.value if s.ai_suggested else None,
                        "ai_followed": s.ai_suggested is s.transition,
                    }
                    for s in case_file.history
                ],
                "transitions": [
                    {
                        "value": t.value,
                        "label": t.value.replace("dispose_", "").replace("_", " "),
                        "needs_reason": (
                            DISPOSITION_OF[t].requires_reason if t in DISPOSITION_OF else False
                        ),
                        "disposing": t in DISPOSITION_OF,
                    }
                    for t in sorted(case_file.legal_transitions(), key=lambda t: t.value)
                ],
                "closed": case_file.is_closed,
            },
        )

    @app.post("/cases/{case_id}/transition", response_class=HTMLResponse)
    async def apply_transition(
        request: Request,
        case_id: str,
        transition: Annotated[str, Form()],
        actor: Annotated[str, Form()],
        reason: Annotated[str, Form()] = "",
        ai_suggested: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        repo = repo_of(request)
        case_file = repo.get(case_id)
        if case_file is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")

        try:
            suggested = None
            if ai_suggested:
                if request.app.state.shown_suggestions.get(case_id) != ai_suggested:
                    raise ValueError(
                        "that AI suggestion was not shown for this case; the adoption record "
                        "only accepts suggestions the system actually made"
                    )
                suggested = Transition(ai_suggested)
            moved = case_file.apply(
                Transition(transition),
                actor=actor,
                reason=reason or None,
                ai_suggested=suggested,
            )
        except (IllegalTransitionError, ValueError) as exc:
            # Shown to the operator rather than swallowed. The refusals in the
            # state machine carry their own explanations, and those
            # explanations are the product.
            return TEMPLATES.TemplateResponse(
                request=request,
                name="_error.html",
                context={"message": str(exc)},
                status_code=422,
            )

        repo.save_transition(case_file, moved)
        request.app.state.shown_suggestions.pop(case_id, None)
        return HTMLResponse(
            '<div class="ok">Recorded. '
            f'<a href="/cases/{case_id}">Reload the case</a> or '
            '<a href="/">return to the queue</a>.</div>'
        )

    # -- AI ------------------------------------------------------------

    def model_name(request: Request) -> str | None:
        m = request.app.state.ai_model
        return getattr(m, "name", None) if m is not None else None

    @app.post("/cases/{case_id}/ai/recommend", response_class=HTMLResponse)
    async def ai_recommend(request: Request, case_id: str) -> HTMLResponse:
        case_file = repo_of(request).get(case_id)
        if case_file is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")
        result = recommend(case_file, model=request.app.state.ai_model)
        if request.app.state.ai_model is not None:
            repo_of(request).record_ai_check("suggestion", result.status, result.problem)
        shown = request.app.state.shown_suggestions
        if result.status == "ok" and result.action is not None:
            shown[case_id] = result.action.value
        else:
            shown.pop(case_id, None)
        while len(shown) > 1000:
            shown.pop(next(iter(shown)))
        return TEMPLATES.TemplateResponse(
            request=request, name="_ai_recommendation.html", context={"r": result}
        )

    @app.post("/cases/{case_id}/ai/draft", response_class=HTMLResponse)
    async def ai_draft(request: Request, case_id: str) -> HTMLResponse:
        case_file = repo_of(request).get(case_id)
        if case_file is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")
        result = draft_reply(case_file, model=request.app.state.ai_model)
        if request.app.state.ai_model is not None:
            repo_of(request).record_ai_check("draft", result.status, result.problem)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="_ai_draft.html",
            context={"d": result, "formal": formal_reply(case_file)},
        )

    @app.get("/intake", response_class=HTMLResponse)
    async def intake_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="intake.html",
            context={
                **base_context(request),
                "ai_on": request.app.state.ai_model is not None,
                "model_name": model_name(request),
                "sample": SAMPLE_REQUEST,
            },
        )

    @app.post("/intake/extract", response_class=HTMLResponse)
    async def intake_extract(
        request: Request,
        text: Annotated[str, Form()] = "",
        document: UploadFile | None = None,
    ) -> HTMLResponse:
        if document is not None and document.filename:
            try:
                text = text_from_upload(document.filename, await document.read())
            except DocumentError as unreadable:
                return TEMPLATES.TemplateResponse(
                    request=request,
                    name="_error.html",
                    context={"message": str(unreadable)},
                    status_code=422,
                )
        if not text.strip():
            return TEMPLATES.TemplateResponse(
                request=request,
                name="_error.html",
                context={"message": "Paste a message or upload a PDF."},
                status_code=422,
            )
        if len(text) > 20_000:
            return TEMPLATES.TemplateResponse(
                request=request,
                name="_error.html",
                context={"message": "Message too long; paste the relevant part."},
                status_code=422,
            )
        draft = ClaudeExtractor(request.app.state.ai_model, from_environment=False).extract(text)
        if request.app.state.ai_model is not None:
            _log_intake_checks(repo_of(request), draft)
        bank = suggest_requesting_bank(text, exclude=request.app.state.config.institution_id)
        token = secrets.token_urlsafe(12)
        drafts = request.app.state.drafts
        drafts[token] = draft
        while len(drafts) > 200:  # bounded: drafts are held in memory until filed
            drafts.pop(next(iter(drafts)))

        def pre(name: str) -> str:
            f = draft.fields.get(name)
            return f.value if f else ""

        amount_cents = pre("amount_cents")
        return TEMPLATES.TemplateResponse(
            request=request,
            name="_intake_draft.html",
            context={
                "fields": sorted(draft.fields.items()),
                "source_text": text,
                "bank": bank,
                "abstained": draft.abstained,
                "notes": draft.notes,
                "token": token,
                "rails": [r.value for r in Rail],
                "reasons": [r.value for r in RecallReason],
                "pre": {
                    "rail": pre("rail"),
                    "amount": f"{int(amount_cents) / 100:.2f}" if amount_cents else "",
                    "reason": pre("reason"),
                    "reference": pre("original_payment_reference"),
                },
            },
        )

    @app.post("/intake/confirm", response_class=HTMLResponse)
    async def intake_confirm(
        request: Request,
        token: Annotated[str, Form()],
        rail: Annotated[str, Form()],
        amount: Annotated[str, Form()],
        reason: Annotated[str, Form()],
        reference: Annotated[str, Form()],
        requesting: Annotated[str, Form()],
        operator: Annotated[str, Form()],
    ) -> HTMLResponse:
        draft = request.app.state.drafts.get(token)
        if draft is None:
            return TEMPLATES.TemplateResponse(
                request=request,
                name="_error.html",
                context={"message": "That draft has expired. Extract the message again."},
                status_code=422,
            )
        now = datetime.now(UTC)
        case_id = f"ILK-{now:%Y-%m%d}-{secrets.randbelow(10**6):06d}"
        try:
            case = draft.confirm(
                confirmed_by=operator,
                case_id=case_id,
                rail=Rail(rail),
                requesting_institution_id=requesting,
                responding_institution_id=request.app.state.config.institution_id,
                amount_cents=cents_from_decimal(Decimal(amount.replace(",", "").lstrip("$"))),
                reason=RecallReason(reason),
                original_payment_reference=reference,
            )
            repo_of(request).open_new(case)
        except (ValueError, ArithmeticError, ValidationError) as refused:
            return TEMPLATES.TemplateResponse(
                request=request,
                name="_error.html",
                context={"message": f"Not filed: {refused}"},
                status_code=422,
            )
        request.app.state.drafts.pop(token, None)
        return HTMLResponse(
            f'<div class="ok">Filed as <a href="/cases/{case_id}">{case_id}</a>, confirmed by '
            f"{escape(operator.strip())}. Its deadline clock has started.</div>"
        )

    @app.get("/ai", response_class=HTMLResponse)
    async def ai_page(request: Request) -> HTMLResponse:
        ai_on = request.app.state.ai_model is not None
        report = evaluate_extractor(KeywordExtractor(), EXTENDED_CORPUS)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="ai.html",
            context={
                **base_context(request),
                "ai_on": ai_on,
                "model_name": model_name(request),
                "a": adoption(repo_of(request).audit_entries()),
                "checks": _check_table(repo_of(request).ai_check_counts()),
                "problems": repo_of(request).ai_check_problems(),
                "extraction_source": "rules-based extractor (runs free on every page load)",
                "extraction_report": report.render(),
            },
        )

    # -- evidence ------------------------------------------------------

    @app.get("/evidence", response_class=HTMLResponse)
    async def evidence_page(request: Request) -> HTMLResponse:
        repo = repo_of(request)
        cases = repo.all_cases()
        entries = repo.audit_entries()
        now = datetime.now(UTC)

        export = build_export(
            cases=cases,
            entries=entries,
            period_start=now - timedelta(days=90),
            period_end=now,
            institution_id=request.app.state.config.institution_id,
        )

        return TEMPLATES.TemplateResponse(
            request=request,
            name="evidence.html",
            context={
                **base_context(request),
                "export": export,
                "independently_verified": not verify_export(export),
                "chain_tail": export["chain"][-8:],
            },
        )

    @app.get("/evidence.json")
    async def evidence_json(request: Request) -> JSONResponse:
        """The export itself, for an examiner to take away and verify."""
        repo = repo_of(request)
        now = datetime.now(UTC)
        export = build_export(
            cases=repo.all_cases(),
            entries=repo.audit_entries(),
            period_start=now - timedelta(days=90),
            period_end=now,
            institution_id=request.app.state.config.institution_id,
        )
        return JSONResponse(export)

    # -- rails ---------------------------------------------------------

    @app.get("/rails", response_class=HTMLResponse)
    async def rails_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="rails.html",
            context={
                **base_context(request),
                "rails": [
                    {
                        "rail": rail.value,
                        "profile": profile_for(rail),
                        "binding": profile_for(rail).has_enforceable_deadline,
                    }
                    for rail in sorted(Rail, key=lambda r: r.value)
                ],
            },
        )

    return app


app = create_app()
