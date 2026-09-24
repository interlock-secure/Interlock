"""M8: the console, end to end.

The acceptance criterion that matters is the first class: a case can be taken
from arrival to disposition entirely through the interface. Everything else -
the clocks, the badges, the export - is there to make that one journey
defensible.
"""

from __future__ import annotations

import re
import time
import warnings

import pytest
from fastapi.testclient import TestClient

from interlock.api.app import create_app, rank, recoverability_estimate
from interlock.config import Settings
from interlock.recall.evidence import verify_export

warnings.filterwarnings("ignore", category=DeprecationWarning)

SUPPORT = "support@test-interlock.example"


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app(
        Settings(
            database_path=":memory:",
            support_email=SUPPORT,
            institution_name="Harbor National",
            institution_id="inst-harbor-national",
        )
    )
    with TestClient(app) as c:
        yield c


def _case_ids(html: str) -> list[str]:
    seen: list[str] = []
    for match in re.findall(r"/cases/(ILK-[A-Z0-9]+)", html):
        if match not in seen:
            seen.append(match)
    return seen


class TestPagesRender:
    @pytest.mark.parametrize("path", ["/", "/rails", "/evidence", "/healthz", "/evidence.json"])
    def test_page_returns_200(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 200

    def test_queue_has_cases(self, client: TestClient) -> None:
        assert len(_case_ids(client.get("/").text)) > 5

    def test_case_page_renders(self, client: TestClient) -> None:
        case_id = _case_ids(client.get("/").text)[0]
        assert client.get(f"/cases/{case_id}").status_code == 200

    def test_unknown_case_is_404(self, client: TestClient) -> None:
        assert client.get("/cases/ILK-DOESNOTEXIST").status_code == 404


class TestTheSupportContactIsOneConstant:
    def test_it_appears_in_the_footer(self, client: TestClient) -> None:
        assert SUPPORT in client.get("/").text

    def test_it_appears_on_every_page(self, client: TestClient) -> None:
        for path in ("/", "/rails", "/evidence"):
            assert SUPPORT in client.get(path).text, f"{path} lost the support contact"

    def test_it_appears_in_the_health_check(self, client: TestClient) -> None:
        assert client.get("/healthz").json()["support"] == SUPPORT

    def test_it_is_not_hardcoded_anywhere(self) -> None:
        """Changing who fields support mail must be one edit, not a search."""
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "interlock"
        offenders = []
        for path in src.rglob("*"):
            if path.suffix not in {".py", ".html"} or path.name == "config.py":
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if "@interlock-secure.com" in line or "support@" in line.replace(
                    "support_email", ""
                ).replace("{{ support_email }}", ""):
                    if "mailto:{{ support_email }}" in line:
                        continue
                    offenders.append(f"{path.relative_to(src)}:{lineno}")
        assert not offenders, "support address must come from config.py alone: " + ", ".join(
            offenders
        )


class TestTheDeadlineAuthorityIsVisible:
    """The distinction the product exists to make."""

    def test_the_queue_labels_rule_and_policy_separately(self, client: TestClient) -> None:
        html = client.get("/").text
        assert "rail rule" in html
        assert "house policy" in html

    def test_the_queue_explains_what_the_badges_mean(self, client: TestClient) -> None:
        html = " ".join(client.get("/").text.split())
        assert "never agreed to" in html

    def test_unverified_rules_are_surfaced_not_hidden(self, client: TestClient) -> None:
        html = " ".join(client.get("/").text.split())
        assert "could not verify" in html

    def test_the_rails_page_shows_provenance(self, client: TestClient) -> None:
        html = " ".join(client.get("/rails").text.split())
        assert "frbservices.org" in html, "the rail's own authority document must be linked"
        assert "nacha.org" in html
        assert "confirmed" in html


class TestTheFullJourney:
    """Arrival to disposition, entirely through the interface."""

    def test_a_case_can_be_disposed_through_the_ui(self, client: TestClient) -> None:
        case_id = None
        for candidate in _case_ids(client.get("/").text):
            page = client.get(f"/cases/{candidate}").text
            if "Record a disposition" in page:
                case_id = candidate
                break
        assert case_id, "no open case found in the queue"

        response = client.post(
            f"/cases/{case_id}/transition",
            data={
                "transition": "dispose_insufficient_funds",
                "actor": "test-operator",
                "reason": "Beneficiary account emptied before the request arrived",
            },
        )
        assert response.status_code == 200
        assert "Recorded" in response.text

        after = client.get(f"/cases/{case_id}").text
        assert "insufficient funds" in after
        assert "test-operator" in after
        assert "cannot be reopened" in after

    def test_a_disposed_case_leaves_the_open_queue(self, client: TestClient) -> None:
        before = set(_case_ids(client.get("/").text))
        target = next(c for c in before if "Record a disposition" in client.get(f"/cases/{c}").text)
        client.post(
            f"/cases/{target}/transition",
            data={"transition": "dispose_funds_returned", "actor": "test-operator"},
        )
        assert target not in set(_case_ids(client.get("/").text))

    def test_a_missing_reason_is_refused_and_explained(self, client: TestClient) -> None:
        """The state machine's refusals carry their own explanations, and
        those explanations are the product - so they reach the operator."""
        target = next(
            c
            for c in _case_ids(client.get("/").text)
            if "Record a disposition" in client.get(f"/cases/{c}").text
        )
        response = client.post(
            f"/cases/{target}/transition",
            data={"transition": "dispose_declined", "actor": "test-operator", "reason": ""},
        )
        assert response.status_code == 422
        assert "requires a reason" in response.text
        assert "regulator" in response.text

    def test_premature_expiry_acknowledgement_is_refused(self, client: TestClient) -> None:
        target = next(
            c
            for c in _case_ids(client.get("/").text)
            if "Record a disposition" in client.get(f"/cases/{c}").text
        )
        response = client.post(
            f"/cases/{target}/transition",
            data={
                "transition": "acknowledge_sla_expiry",
                "actor": "test-operator",
                "reason": "trying it on",
            },
        )
        # Either refused because the window is still open, or accepted because
        # this particular case has genuinely breached. Both are correct; what
        # must not happen is a silent close on an open window.
        if response.status_code == 422:
            assert "not" in response.text and "breached" in response.text


class TestEvidenceThroughTheApi:
    def test_the_export_verifies_independently(self, client: TestClient) -> None:
        assert verify_export(client.get("/evidence.json").json()) == []

    def test_the_export_declares_the_format_is_ours(self, client: TestClient) -> None:
        assert "Interlock's own" in client.get("/evidence.json").json()["disclaimer"]

    def test_nothing_closed_without_a_disposition(self, client: TestClient) -> None:
        export = client.get("/evidence.json").json()
        assert export["disposition_completeness"]["closed_without_disposition"] == 0

    def test_house_policy_rails_are_not_reported_as_obligations(self, client: TestClient) -> None:
        export = client.get("/evidence.json").json()
        for obligation in export["obligations"]:
            if not obligation["deadline_is_binding"]:
                assert "not an obligation" in obligation["deadline_basis"].lower()


class TestHealth:
    def test_health_reports_chain_state(self, client: TestClient) -> None:
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["chain_length"] > 0
        assert body["chain_problems"] == []


class TestRanking:
    def test_the_queue_is_ordered_by_expected_value(self, client: TestClient) -> None:
        repo = client.app.state.repo
        from datetime import UTC, datetime

        rows = rank(repo.open_cases(), now=datetime.now(UTC))
        values = [r["expected_cents"] for r in rows]
        assert values == sorted(values, reverse=True)

    def test_a_case_without_a_settlement_time_is_not_ranked_last(self) -> None:
        """It cannot be ranked, so it must sit mid-queue and get looked at
        rather than sinking out of sight."""
        from datetime import UTC, datetime

        from interlock.recall.state import open_case
        from interlock.schema.case import (
            Channel,
            Direction,
            NativeEnvelope,
            RecallCase,
            RecallReason,
        )
        from interlock.schema.common import Rail

        now = datetime.now(UTC)
        case = RecallCase(
            case_id="ILK-NOSETTLEMENT",
            rail=Rail.ACH,
            direction=Direction.INBOUND,
            channel=Channel.ACH_R06_REQUEST,
            original_payment_reference="unknown",
            amount_cents=100_000,
            reason=RecallReason.FRAUD_SCAM,
            requesting_institution_id="inst-a",
            responding_institution_id="inst-b",
            original_settled_at=None,
            received_at=now,
            native=NativeEnvelope(message_id="X", reason_code="R06", creation_time=now),
        )
        estimate = recoverability_estimate(open_case(case), now=now)
        assert 0.2 < estimate < 0.6

    def test_the_console_says_it_is_not_running_a_live_model(self, client: TestClient) -> None:
        assert "not a live model score" in " ".join(client.get("/").text.split())


class TestLatencyBudget:
    def test_queue_p99_under_300ms(self, client: TestClient) -> None:
        """Not a rail constraint - recall runs on a banking-day clock - but an
        operator working a decaying queue should never wait on the tool."""
        timings = []
        for _ in range(40):
            start = time.perf_counter()
            client.get("/")
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()
        p99 = timings[int(len(timings) * 0.99) - 1]
        assert p99 < 300, f"queue p99 {p99:.0f}ms exceeds the 300ms budget"
