"""M7: is the extractor measured in a way that would catch a bad model?

The harness matters more than the extractor it currently scores. When a
model-backed extractor replaces the deterministic one, these are the tests
that decide whether it is safe to use.
"""

from __future__ import annotations

import pytest

from interlock.adapters.extraction_eval import (
    CORPUS,
    ExtractionReport,
    evaluate_extractor,
)
from interlock.adapters.freetext import KeywordExtractor


@pytest.fixture(scope="module")
def report() -> ExtractionReport:
    return evaluate_extractor(KeywordExtractor())


class TestTheCorpusIsWorthScoringAgainst:
    def test_it_contains_hostile_messages(self) -> None:
        assert sum(1 for m in CORPUS if m.hostile) >= 2

    def test_it_contains_a_message_that_is_not_a_recall(self) -> None:
        """Otherwise nothing tests whether the extractor knows when to say
        nothing at all."""
        assert any(m.expected == {} for m in CORPUS)

    def test_it_contains_an_ambiguous_amount(self) -> None:
        assert any("fee of $" in m.text for m in CORPUS)

    def test_every_message_expects_abstention_somewhere(self) -> None:
        """Institution identity is never stated in a way worth trusting."""
        for message in CORPUS:
            assert message.must_abstain, f"{message.name} pins down nothing to abstain on"


class TestScoringBehaviour:
    def test_abstention_is_not_counted_as_an_error(self, report: ExtractionReport) -> None:
        institution = report.fields["requesting_institution_id"]
        assert institution.wrong == 0
        assert institution.hallucinated == 0
        assert institution.abstained_correctly == len(CORPUS)
        assert institution.precision_when_answering is None

    def test_per_field_results_are_not_averaged(self, report: ExtractionReport) -> None:
        """An extractor that nails amounts and mangles institutions has a good
        average and is dangerous."""
        assert len(report.fields) >= 5
        assert all(hasattr(s, "precision_when_answering") for s in report.fields.values())

    def test_hallucination_is_tracked_separately_from_being_wrong(
        self, report: ExtractionReport
    ) -> None:
        assert hasattr(report, "total_hallucinations")


class TestTheCurrentExtractor:
    def test_it_hallucinates_nothing(self, report: ExtractionReport) -> None:
        """A value invented for a field the text does not support starts a
        formal claim against a customer's account on no evidence."""
        assert report.total_hallucinations == 0

    def test_it_resists_every_injection(self, report: ExtractionReport) -> None:
        assert report.injection_resistance == 1.0

    def test_it_is_precise_where_it_answers(self, report: ExtractionReport) -> None:
        for name, score in report.fields.items():
            if score.answered:
                assert score.precision_when_answering == 1.0, f"{name} answered wrongly"

    def test_negated_fraud_is_not_a_reason(self) -> None:
        """The defect the harness caught on its first run.

        "No fraud involved, just a posting error" was classified as a scam
        because the word 'fraud' appears in it.
        """
        draft = KeywordExtractor().extract(
            "No fraud involved, just a posting error on our side. Nothing needed."
        )
        assert "reason" not in draft.fields
        assert "reason" in draft.abstained
        assert any("negation" in n for n in draft.notes)

    def test_a_denial_followed_by_a_claim_still_extracts(self) -> None:
        """The negation window must not swallow a real claim in the next
        sentence."""
        draft = KeywordExtractor().extract(
            "There was no error on our side. Our member was scammed out of $200.00."
        )
        assert draft.fields["reason"].value == "fraud_scam"

    def test_an_ambiguous_amount_is_flagged_for_review(self) -> None:
        draft = KeywordExtractor().extract(
            "Member tricked into sending $9,300.00. Our fee of $25.00 applies."
        )
        assert "amount_cents" in draft.needs_review


class TestReportRendering:
    def test_report_says_how_to_score_the_ai(self, report: ExtractionReport) -> None:
        """Which extractor was scored, and how to score the other, is stated."""
        rendered = report.render()
        assert "interlock.ai.evaluate" in rendered

    def test_report_explains_why_abstention_is_not_an_error(self, report: ExtractionReport) -> None:
        assert "always guesses" in report.render()

    def test_report_restates_the_human_gate(self, report: ExtractionReport) -> None:
        assert "named operator" in report.render()

    def test_report_has_a_row_per_field(self, report: ExtractionReport) -> None:
        rendered = report.render()
        for name in report.fields:
            assert name in rendered
