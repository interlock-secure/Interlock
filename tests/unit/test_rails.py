"""The capability matrix has to be the only place rail rules live.

Two kinds of test here. The first kind checks the matrix says what the research
found. The second kind - and the more valuable one - checks that nothing else in
the codebase has quietly grown its own copy of a rule.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from interlock.schema.common import Rail
from interlock.schema.rails import (
    RAIL_PROFILES,
    DispositionFormat,
    ResponseObligation,
    SourceConfidence,
    UnverifiedRuleError,
    WindowUnit,
    house_policy_window,
    profile_for,
    rails_with_enforceable_deadlines,
    render_matrix_markdown,
    require_verified_window,
    unverified_rules,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "interlock"


class TestTheMatrixMatchesTheResearch:
    def test_every_rail_has_a_profile(self) -> None:
        assert set(RAIL_PROFILES) == set(Rail)

    def test_ach_has_the_obligation_and_no_format(self) -> None:
        """The central asymmetry. If this test changes, the product thesis changed."""
        ach = profile_for(Rail.ACH)
        assert ach.obligation is ResponseObligation.MANDATORY
        assert ach.disposition_format is DispositionFormat.UNSTRUCTURED
        assert ach.window.amount == 10
        assert ach.window.unit is WindowUnit.BANKING_DAYS
        assert ach.window.provenance.confidence is SourceConfidence.CONFIRMED

    def test_instant_rails_have_the_format_and_no_enforceable_obligation(self) -> None:
        for rail in (Rail.FEDNOW, Rail.FEDWIRE):
            profile = profile_for(rail)
            assert profile.disposition_format is DispositionFormat.STRUCTURED
            assert profile.obligation is ResponseObligation.ADVISORY, (
                f"{rail.value}: Operating Circular 8 says 'should', not 'shall'"
            )
            assert not profile.has_enforceable_deadline

    def test_exactly_one_rail_has_an_enforceable_deadline(self) -> None:
        """ACH, and only ACH.

        RTP is excluded despite a mandatory obligation because its window
        reportedly exempts fraud claims, and fraud is the only thing Interlock
        handles. A window that does not cover our cases is not a deadline.
        """
        assert rails_with_enforceable_deadlines() == (Rail.ACH,)

    def test_rtp_fraud_carve_out_is_recorded_as_unconfirmed(self) -> None:
        rtp = profile_for(Rail.RTP)
        assert rtp.window.fraud_exempt
        assert rtp.window.provenance.confidence is SourceConfidence.LIKELY
        assert rtp.window.provenance.note, "An unconfirmed rule must explain why"

    def test_fednow_runs_house_policy_and_admits_it(self) -> None:
        fednow = profile_for(Rail.FEDNOW)
        assert fednow.window_is_house_policy
        assert fednow.window.unit is WindowUnit.HOURS


class TestProvenanceIsMandatory:
    @pytest.mark.parametrize("rail", list(Rail))
    def test_every_window_cites_a_source(self, rail: Rail) -> None:
        provenance = profile_for(rail).window.provenance
        assert provenance.source_url.startswith("https://")
        assert provenance.claim.strip()
        assert provenance.retrieved is not None

    def test_unverified_rules_are_enumerable(self) -> None:
        """A reviewer should see the project's epistemic debt in one call."""
        listed = {rail for rail, _ in unverified_rules()}
        assert Rail.RTP in listed, "RTP's window rests on a secondary source"
        assert Rail.ACH not in listed, "ACH's rule is quoted from nacha.org"


class TestUnverifiedRulesCannotDriveDeadlines:
    def test_ach_window_is_returned(self) -> None:
        window = require_verified_window(Rail.ACH, is_fraud_claim=True)
        assert window.amount == 10

    def test_rtp_fraud_claim_refuses(self) -> None:
        with pytest.raises(UnverifiedRuleError, match="carved out"):
            require_verified_window(Rail.RTP, is_fraud_claim=True)

    def test_rtp_non_fraud_claim_also_refuses_on_confidence(self) -> None:
        """Even without the carve-out, the window itself was never seen at source."""
        with pytest.raises(UnverifiedRuleError, match="likely"):
            require_verified_window(Rail.RTP, is_fraud_claim=False)

    def test_house_policy_is_available_but_labelled(self) -> None:
        window = house_policy_window()
        assert window.unit is WindowUnit.HOURS
        assert "house policy" in window.provenance.claim.lower()
        assert window.provenance.note and "ours" in window.provenance.note.lower()

    def test_banking_day_windows_refuse_to_become_timedeltas(self) -> None:
        """Ten banking days is not 240 hours and the type system should say so."""
        with pytest.raises(ValueError, match="banking calendar"):
            profile_for(Rail.ACH).window.as_timedelta()


class TestNoRuleIsHardcodedElsewhere:
    """The acceptance criterion from BUILD_PLAN M3.

    A rail deadline written anywhere but rails.py is the bug this module exists
    to prevent, and it is the kind that survives review because each individual
    occurrence looks harmless.
    """

    def test_no_module_defines_its_own_deadline_constant(self) -> None:
        """Walk the AST rather than grepping.

        A first version of this test grepped for "ten banking days" and
        "timedelta(hours=" and drowned in false positives: prose in docstrings
        explaining the Nacha rule, and the generator's mule sweep delays, which
        are behavioural parameters and nothing to do with rail rules.

        It did however find one real offender - a DEFAULT_RECALL_SLA constant in
        recall.py - which is why the test survived in this narrower form rather
        than being deleted as noise.

        The rule enforced: no module outside rails.py may bind a name that reads
        like a deadline to a literal duration.
        """
        deadline_name = re.compile(r"(SLA|WINDOW|DEADLINE|TIMEOUT|EXPIR|GRACE)", re.IGNORECASE)

        # Durations that are named like a deadline and are not one. Each is
        # listed with its reason rather than renamed to dodge the check: the
        # names are correct, and an allowlist a reviewer can read beats a
        # constant renamed to something vaguer.
        allowed = {
            ("network/peer.py", "REPLAY_WINDOW"): (
                "how stale a signed peer request may be - a replay tolerance, "
                "not a response deadline owed to a counterparty"
            ),
            ("ai/client.py", "REQUEST_TIMEOUT_SECONDS"): (
                "how long to wait for the language model API before reporting it "
                "unavailable - a network timeout, not a rail rule"
            ),
        }

        offenders: list[str] = []

        for path in sorted(SRC.rglob("*.py")):
            if path.name == "rails.py":
                continue

            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign | ast.AnnAssign):
                    continue

                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if not any(deadline_name.search(n) for n in names):
                    continue

                value = node.value
                if value is None:
                    continue

                # A literal number, or a timedelta built from literals.
                is_literal_duration = isinstance(value, ast.Constant) and isinstance(
                    value.value, int | float
                )
                is_timedelta_call = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "timedelta"
                )

                if not (is_literal_duration or is_timedelta_call):
                    continue

                relative = str(path.relative_to(SRC))
                if any((relative, n) in allowed for n in names):
                    continue

                offenders.append(f"{relative}:{node.lineno}: {', '.join(names)}")

        assert not offenders, (
            "A deadline must come from interlock.schema.rails, with its provenance, "
            "never from a literal:\n" + "\n".join(offenders)
        )


class TestDocumentationCannotGoStale:
    def test_checked_in_matrix_matches_the_code(self) -> None:
        """Regenerate with:

            uv run python -c "from interlock.schema.rails import render_matrix_markdown; \
from pathlib import Path; \
Path('docs/protocol/rails.md').write_text(render_matrix_markdown())"
        """
        rendered = render_matrix_markdown()
        checked_in = (SRC.parents[1] / "docs" / "protocol" / "rails.md").read_text()
        assert checked_in == rendered, (
            "docs/protocol/rails.md has drifted from the matrix; regenerate it"
        )

    def test_house_policy_is_not_presented_as_a_confirmed_rail_rule(self) -> None:
        """The confidence column describes the rail's rule, not our fallback."""
        rendered = render_matrix_markdown()
        fednow_row = next(line for line in rendered.splitlines() if line.startswith("| **fednow**"))
        assert "CONFIRMED" not in fednow_row
        assert "house policy" in fednow_row
