"""Two Interlock instances talking directly, and why this is last.

The upgrade, not the product
----------------------------
When two institutions both run Interlock, their requests can stop going by
email and go machine to machine, with a structured disposition coming back.
That is the v1.1 vision - reached by a route that did not require it to exist
first.

This is deliberately the final milestone and it is explicitly optional. Every
earlier component works with zero counterparties, and a test in this module's
suite asserts that turning networking off leaves the rest of the system
untouched. If this milestone had been built first, as v1.1 proposed, the
product would have been worth nothing until a second bank agreed to join.

What this is not
----------------
Not a consortium. There is no discovery service, no registry, no governance,
no shared operator. Two peers are configured with each other's address and a
shared secret, and that is the whole trust model. Anything larger is a
different project with different problems, and pretending otherwise is how
the v1.1 framing went wrong.

Authentication
--------------
HMAC over the request body with a per-peer shared secret, compared in constant
time. Deliberately simple and deliberately not a bearer token: a token in a
header is replayable by anyone who sees one, whereas a body signature plus a
timestamp binds the credential to the specific message. Mutual TLS would be
the production answer; this is the smallest thing that is not obviously wrong,
and it says so.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from interlock.schema.case import RecallCase
from interlock.schema.versioning import SUPPORTED_VERSIONS, negotiate

SIGNATURE_HEADER = "x-interlock-signature"
VERSION_HEADER = "x-interlock-version"
TIMESTAMP_HEADER = "x-interlock-timestamp"

REPLAY_WINDOW = timedelta(minutes=5)
"""How far out of date a signed request may be.

Without this the signature is replayable forever: an observer who captures one
valid exchange can resend it indefinitely, and on a system whose entire job is
recording who asked for what and when, a duplicate request is a duplicate
claim against a customer's account.
"""


class PeerAuthError(RuntimeError):
    """A request that did not authenticate. Never says which check failed.

    A caller learning that the signature was right but the timestamp stale
    learns their key is valid, which is exactly what an attacker probing with
    a guessed secret wants to know.
    """


@dataclass(frozen=True, slots=True)
class Peer:
    """One configured counterparty running Interlock."""

    institution_id: str
    base_url: str
    shared_secret: str

    def sign(self, body: bytes, *, timestamp: str) -> str:
        """Sign a request body together with its timestamp.

        The timestamp is inside the signature rather than beside it. Signing
        the body alone would let an observer replay a valid message with a
        fresh timestamp, which defeats the replay window entirely.
        """
        material = timestamp.encode("utf-8") + b"." + body
        return hmac.new(self.shared_secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def verify_request(
    peer: Peer,
    body: bytes,
    *,
    signature: str,
    timestamp: str,
    now: datetime | None = None,
) -> None:
    """Authenticate an inbound peer request, or raise.

    Raises:
        PeerAuthError: for a bad signature, a stale timestamp, or an
            unparseable one. The message is identical in all three cases.
    """
    now = now or datetime.now(UTC)
    generic = PeerAuthError("Request did not authenticate")

    try:
        sent_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise generic from None

    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)

    if abs(now - sent_at) > REPLAY_WINDOW:
        raise generic

    # compare_digest, not ==. A timing-variable comparison leaks the signature
    # a byte at a time to anyone patient enough to measure.
    if not hmac.compare_digest(peer.sign(body, timestamp=timestamp), signature):
        raise generic


def encode_case(case: RecallCase, *, protocol_version: str) -> bytes:
    """Serialise a case for the wire.

    Sorted keys and fixed separators, so two instances signing the same case
    produce the same bytes and therefore the same signature. Without that, the
    signature would depend on dictionary ordering rather than on content.
    """
    payload = {
        "protocol_version": protocol_version,
        "case": json.loads(case.model_dump_json()),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_case(body: bytes, *, supported: tuple[str, ...] = SUPPORTED_VERSIONS) -> RecallCase:
    """Parse a case from a peer.

    Version is negotiated **before** the body is parsed strictly, which is the
    ordering the protocol design has required since M1: parsing first and
    negotiating afterwards means a peer on a newer minor version gets a
    validation error instead of a version error, and spends a day debugging
    the wrong thing.
    """
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Peer sent unparseable body: {exc}") from exc

    if not isinstance(envelope, dict) or "case" not in envelope:
        raise ValueError("Peer body has no 'case' member")

    theirs = envelope.get("protocol_version")
    if not theirs:
        raise ValueError("Peer body declares no protocol_version")

    # negotiate() raises VersionNegotiationError naming both sides' support,
    # so an operator reading one log can see which end needs upgrading rather
    # than correlating two systems to find out.
    negotiate(supported, [theirs])

    return RecallCase.model_validate(envelope["case"])
