"""Protocol version negotiation.

A participant that upgrades should not be able to break one that has not. The
specification requires that a lagging participant degrades in capability rather
than in availability (Appendix B).

There is a design tension here worth stating plainly, because the obvious
implementation is wrong. The specification says unknown fields should be
ignored rather than rejected, which suggests permissive parsing. But every wire
model sets ``extra="forbid"``, because permissive parsing is exactly how a
field nobody declared - and therefore nobody privacy-reviewed - ends up
crossing an institutional boundary.

Both requirements are satisfied by negotiating first and parsing strictly
afterwards: the sender is responsible for speaking the agreed version, rather
than the receiver being responsible for tolerating whatever arrives. A newer
participant downgrades its message before sending. Nothing is silently
dropped, and nothing undeclared is silently carried.
"""

from __future__ import annotations

import re
from functools import total_ordering

_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)$")


@total_ordering
class ProtocolVersion:
    """A major.minor protocol version.

    Major changes are breaking and are not negotiable: a 1.x participant and a
    2.x participant have no common language and the exchange fails loudly
    rather than degrading into a misunderstanding. Minor changes are additive
    and negotiate downward.
    """

    __slots__ = ("major", "minor")

    def __init__(self, major: int, minor: int) -> None:
        if major < 0 or minor < 0:
            raise ValueError("Version components must be non-negative")
        self.major = major
        self.minor = minor

    @classmethod
    def parse(cls, raw: str) -> ProtocolVersion:
        match = _VERSION_PATTERN.match(raw.strip())
        if not match:
            raise ValueError(
                f"Malformed protocol version {raw!r}; expected 'major.minor' such as '1.0'"
            )
        return cls(int(match.group(1)), int(match.group(2)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    def __repr__(self) -> str:
        return f"ProtocolVersion({self.major}, {self.minor})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProtocolVersion):
            return NotImplemented
        return (self.major, self.minor) == (other.major, other.minor)

    def __lt__(self, other: ProtocolVersion) -> bool:
        if not isinstance(other, ProtocolVersion):
            return NotImplemented
        return (self.major, self.minor) < (other.major, other.minor)

    def __hash__(self) -> int:
        return hash((self.major, self.minor))

    def is_compatible_with(self, other: ProtocolVersion) -> bool:
        """True if the two can talk at all. Same major line is required."""
        return self.major == other.major


class VersionNegotiationError(Exception):
    """Raised when two participants have no version in common.

    Deliberately an exception rather than a degraded response. A major-version
    mismatch is a configuration problem that needs an operator, and papering
    over it would mean two institutions exchanging messages they each interpret
    differently - worse than no exchange at all.
    """


def negotiate(
    ours: list[str] | tuple[str, ...],
    theirs: list[str] | tuple[str, ...],
) -> ProtocolVersion:
    """Select the highest version both participants support.

    Raises VersionNegotiationError if there is no overlap, naming both sides'
    support so the operator reading the log can see which one needs upgrading
    rather than having to correlate two systems' logs to find out.
    """
    if not ours or not theirs:
        raise VersionNegotiationError("Both participants must advertise at least one version")

    our_versions = {ProtocolVersion.parse(v) for v in ours}
    their_versions = {ProtocolVersion.parse(v) for v in theirs}

    common = our_versions & their_versions
    if not common:
        our_line = ", ".join(sorted(str(v) for v in our_versions))
        their_line = ", ".join(sorted(str(v) for v in their_versions))
        raise VersionNegotiationError(
            f"No mutually supported protocol version. We support [{our_line}]; "
            f"the counterparty supports [{their_line}]."
        )

    return max(common)


SUPPORTED_VERSIONS: tuple[str, ...] = ("1.0",)
"""Every version this build can speak, oldest first.

Adding a version here is a deliberate act with a checklist attached: the new
version needs golden fixtures, and a round-trip test proving a message written
at the previous version still parses. See tests/unit/test_schema_golden.py.
"""

CURRENT_VERSION: str = SUPPORTED_VERSIONS[-1]
