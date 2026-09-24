"""PDF intake, bank suggestions, rail-format replies and the AI check log."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fpdf import FPDF

from interlock.adapters.directory import suggest_requesting_bank
from interlock.adapters.documents import DocumentError, text_from_upload
from interlock.adapters.iso20022 import Camt029Adapter
from interlock.adapters.responses import ACH_RESPONSE_FORMAT, CAMT029_STATUS_OF, formal_reply
from interlock.recall.state import DISPOSITION_OF, Transition
from interlock.recall.store import SqliteCaseRepository
from interlock.schema.case import Channel
from interlock.schema.common import Rail
from interlock.schema.recall import RecallDispositionCode
from tests.unit.test_ai import EMAIL, FakeModel, _client, extraction, reply, suggestion
from tests.unit.test_ledger_and_evidence import RECEIVED_AT, a_case

NOW = RECEIVED_AT + timedelta(hours=1)


def pdf_with(text: str) -> bytes:
    doc = FPDF()
    doc.add_page()
    doc.set_font("Helvetica", size=11)
    for line in text.splitlines() or [""]:
        doc.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(doc.output())


def blank_pdf() -> bytes:
    doc = FPDF()
    doc.add_page()
    return bytes(doc.output())


class TestDocuments:
    def test_text_comes_out_of_a_pdf(self) -> None:
        text = text_from_upload(
            "letter.pdf", pdf_with("Scammed. $4,820.00. Trace 091000019887766.")
        )
        assert "$4,820.00" in text and "091000019887766" in text

    def test_a_scan_with_no_text_is_refused_not_guessed(self) -> None:
        with pytest.raises(DocumentError, match="no text layer"):
            text_from_upload("scan.pdf", blank_pdf())

    def test_not_a_pdf(self) -> None:
        with pytest.raises(DocumentError, match="Only PDF"):
            text_from_upload("x.docx", b"PK\x03\x04")

    def test_a_corrupt_pdf(self) -> None:
        with pytest.raises(DocumentError, match="could not be read"):
            text_from_upload("x.pdf", b"%PDF-1.7 garbage")

    def test_too_large(self) -> None:
        with pytest.raises(DocumentError, match="5 MB"):
            text_from_upload("x.pdf", b"%PDF" + b"0" * (5 * 1024 * 1024))

    def test_plain_text(self) -> None:
        assert text_from_upload("note.txt", b"call note $50.00") == "call note $50.00"


class TestBankSuggestion:
    def test_sender_domain_match(self) -> None:
        s = suggest_requesting_bank(EMAIL)
        assert s.institution_id == "inst-northbay-cu" and s.basis == "sender domain"

    def test_a_named_bank_is_the_weaker_match(self) -> None:
        s = suggest_requesting_bank("Caller from Pinebrook Bank reported a scam.")
        assert s.institution_id == "inst-pinebrook-bank" and s.basis == "name mentioned"

    def test_unknown_domain_and_no_name_suggests_nothing(self) -> None:
        assert suggest_requesting_bank("From: a@unknown.example\nScam $5.00") is None

    def test_two_named_banks_is_ambiguous(self) -> None:
        assert suggest_requesting_bank("Pinebrook Bank and Cedar Trust both wrote.") is None

    def test_we_are_never_suggested_as_the_requester(self) -> None:
        text = "Dear Harbor National, please return the funds."
        assert suggest_requesting_bank(text, exclude="inst-harbor-national") is None

    def test_the_console_suggests_but_marks_it_for_confirmation(self) -> None:
        with _client(None) as client:
            drafted = client.post("/intake/extract", data={"text": EMAIL})
            assert 'value="inst-northbay-cu"' in drafted.text
            assert "suggested from sender domain" in drafted.text and "confirm" in drafted.text


class TestFormalReplies:
    def _closed(self, rail: Rail, transition: Transition):
        channel = Channel.ACH_R06_REQUEST if rail is Rail.ACH else Channel.CAMT_056
        case = SqliteCaseRepository(":memory:").open_new(a_case(rail=rail, channel=channel))
        reason = "Basis stated" if DISPOSITION_OF[transition].requires_reason else None
        return case.apply(transition, actor="op", reason=reason, at=NOW)

    @pytest.mark.parametrize("transition", sorted(DISPOSITION_OF, key=lambda t: t.value))
    def test_every_outcome_has_a_camt029_that_parses_back(self, transition) -> None:
        if transition is Transition.ACKNOWLEDGE_SLA_EXPIRY:
            pytest.skip("expiry needs a breached deadline; see the test after this one")
        closed = self._closed(Rail.FEDNOW, transition)
        reply_ = formal_reply(closed)
        status, _ = Camt029Adapter(Rail.FEDNOW).parse(reply_.content.encode(), against=closed.case)
        assert status == CAMT029_STATUS_OF[closed.disposition]

    def test_an_acknowledged_expiry_is_a_rejection(self) -> None:
        case = SqliteCaseRepository(":memory:").open_new(
            a_case(rail=Rail.FEDNOW, channel=Channel.CAMT_056)
        )
        late = case.deadline.due_at + timedelta(minutes=1)
        closed = case.apply(
            Transition.ACKNOWLEDGE_SLA_EXPIRY, actor="op", reason="No answer in time", at=late
        )
        reply_ = formal_reply(closed)
        status, info = Camt029Adapter(Rail.FEDNOW).parse(
            reply_.content.encode(), against=closed.case
        )
        assert status == "RJCR" and info == "No answer in time"

    def test_the_mapping_covers_every_disposition(self) -> None:
        assert set(CAMT029_STATUS_OF) == set(RecallDispositionCode)

    def test_frozen_is_pending_and_says_so(self) -> None:
        reply_ = formal_reply(self._closed(Rail.RTP, Transition.DISPOSE_FUNDS_FROZEN))
        assert "PDCR" in reply_.note and "still owed" in reply_.note

    def test_ach_gets_our_labelled_json(self) -> None:
        reply_ = formal_reply(self._closed(Rail.ACH, Transition.DISPOSE_FUNDS_RETURNED))
        body = json.loads(reply_.content)
        assert body["format"] == ACH_RESPONSE_FORMAT and body["outcome"] == "funds_returned"
        assert "not a Nacha standard" in reply_.note

    def test_no_formal_reply_before_an_outcome(self) -> None:
        with pytest.raises(ValueError):
            formal_reply(SqliteCaseRepository(":memory:").open_new(a_case()))


class TestTheCheckLog:
    def test_dropped_intake_fields_are_counted_and_shown(self) -> None:
        bad = extraction(amount={"value": "9000.00", "quote": "$9,000.00", "confidence": 0.9})
        with _client(FakeModel(bad)) as client:
            client.post("/intake/extract", data={"text": EMAIL})
            page = client.get("/ai").text
            assert "does not appear in the message" in page

    def test_rejected_suggestions_are_counted(self) -> None:
        with _client(FakeModel(suggestion(cited_facts=["credit_score"]))) as client:
            case_id = client.get("/").text.split('href="/cases/')[1].split('"')[0]
            client.post(f"/cases/{case_id}/ai/recommend")
            assert "cited facts it was not given" in client.get("/ai").text

    def test_no_key_logs_nothing(self) -> None:
        with _client(None) as client:
            client.post("/intake/extract", data={"text": EMAIL})
            assert "no model answers have been checked" in client.get("/ai").text


class TestPdfThroughTheConsole:
    def test_upload_extract_and_see_the_text(self) -> None:
        with _client(None) as client:
            drafted = client.post(
                "/intake/extract",
                files={"document": ("letter.pdf", pdf_with(EMAIL), "application/pdf")},
            )
            assert drafted.status_code == 200 and "482000" in drafted.text

    def test_a_scan_is_refused_on_screen(self) -> None:
        with _client(None) as client:
            refused = client.post(
                "/intake/extract", files={"document": ("scan.pdf", blank_pdf(), "application/pdf")}
            )
            assert refused.status_code == 422 and "OCR" in refused.text

    def test_draft_panel_includes_the_formal_reply(self) -> None:
        body = "Funds frozen. $2,500.00 for ILK-2026-0914-000001."
        with _client(FakeModel(reply(body))) as client:
            case_id = client.get("/").text.split('href="/cases/')[1].split('"')[0]
            client.post(
                f"/cases/{case_id}/transition",
                data={"transition": "dispose_funds_frozen", "actor": "m.ruiz"},
            )
            panel = client.post(f"/cases/{case_id}/ai/draft").text
            assert "generated from the record - not by the AI" in panel
