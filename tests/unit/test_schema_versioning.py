"""Protocol version negotiation.

Specification Appendix B: a lagging participant degrades in capability, not in
availability. These tests cover the negotiation, and the one case where failing
loudly is correct.
"""

from __future__ import annotations

import pytest

from interlock.schema import (
    CURRENT_VERSION,
    SUPPORTED_VERSIONS,
    ProtocolVersion,
    VersionNegotiationError,
    negotiate,
)


class TestParsing:
    def test_round_trips(self):
        assert str(ProtocolVersion.parse("1.4")) == "1.4"

    @pytest.mark.parametrize("bad", ["1", "1.2.3", "v1.0", "", "1.x", "-1.0"])
    def test_malformed_versions_are_rejected(self, bad: str):
        with pytest.raises(ValueError):
            ProtocolVersion.parse(bad)

    def test_ordering_is_numeric_not_lexical(self):
        # "1.10" > "1.9" numerically but "1.10" < "1.9" as strings. Getting
        # this wrong silently downgrades everyone past the ninth minor version.
        assert ProtocolVersion.parse("1.10") > ProtocolVersion.parse("1.9")

    def test_equality_and_hashing(self):
        assert ProtocolVersion(1, 0) == ProtocolVersion(1, 0)
        assert len({ProtocolVersion(1, 0), ProtocolVersion(1, 0)}) == 1


class TestNegotiation:
    def test_picks_the_highest_common_version(self):
        assert str(negotiate(["1.0", "1.1", "1.2"], ["1.0", "1.1"])) == "1.1"

    def test_a_newer_participant_speaks_down_to_an_older_one(self):
        # The upgraded side gives up capability. The lagging side stays
        # available, which is the requirement.
        assert str(negotiate(["1.0", "1.1", "1.2"], ["1.0"])) == "1.0"

    def test_identical_support_negotiates_cleanly(self):
        assert str(negotiate(["1.0"], ["1.0"])) == "1.0"

    def test_no_overlap_raises_rather_than_guessing(self):
        with pytest.raises(VersionNegotiationError):
            negotiate(["1.0"], ["2.0"])

    def test_the_error_names_both_sides(self):
        # An operator reading one system's log should not have to correlate it
        # with the other system's log to find out who needs upgrading.
        with pytest.raises(VersionNegotiationError) as exc:
            negotiate(["1.0", "1.1"], ["2.0"])
        message = str(exc.value)
        assert "1.0" in message and "1.1" in message and "2.0" in message

    def test_empty_support_list_is_an_error(self):
        with pytest.raises(VersionNegotiationError):
            negotiate([], ["1.0"])


class TestMajorVersionsDoNotSilentlyDowngrade:
    """A major mismatch is a configuration problem that needs a human.

    Papering over it would mean two institutions exchanging messages they each
    interpret differently, which is worse than no exchange at all.
    """

    def test_different_majors_are_incompatible(self):
        assert not ProtocolVersion(1, 0).is_compatible_with(ProtocolVersion(2, 0))

    def test_same_major_different_minor_is_compatible(self):
        assert ProtocolVersion(1, 0).is_compatible_with(ProtocolVersion(1, 7))


class TestBuildSupport:
    def test_current_is_the_newest_supported(self):
        newest = max(ProtocolVersion.parse(v) for v in SUPPORTED_VERSIONS)
        assert str(newest) == CURRENT_VERSION

    def test_supported_versions_are_all_parseable(self):
        for version in SUPPORTED_VERSIONS:
            ProtocolVersion.parse(version)

    def test_every_supported_version_has_golden_fixtures(self):
        # Adding a version without fixtures would mean the compatibility test
        # silently covers less than it appears to.
        from pathlib import Path

        fixtures = Path(__file__).parent.parent / "fixtures" / "schema"
        for version in SUPPORTED_VERSIONS:
            assert (fixtures / version).is_dir(), (
                f"Protocol version {version} is advertised in SUPPORTED_VERSIONS but has no "
                f"golden fixtures at tests/fixtures/schema/{version}/. Generate them with "
                "`uv run python -m tests.fixtures.generate` before advertising the version."
            )
