"""M9: two instances exchanging cases, and the guarantee that this is optional.

The most important test here is the last class. Network mode is the upgrade,
not the product, and if turning it off broke anything then the whole v2.0
argument - that value arrives at n=1 - would be false.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from interlock.network.peer import (
    REPLAY_WINDOW,
    Peer,
    PeerAuthError,
    decode_case,
    encode_case,
    verify_request,
)
from interlock.schema.case import (
    Channel,
    Direction,
    NativeEnvelope,
    RecallCase,
    RecallReason,
)
from interlock.schema.common import Rail
from interlock.schema.versioning import CURRENT_VERSION, VersionNegotiationError

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


def a_case() -> RecallCase:
    return RecallCase(
        case_id="ILK-2026-0919-000001",
        rail=Rail.FEDNOW,
        direction=Direction.OUTBOUND,
        channel=Channel.CAMT_056,
        original_payment_reference="E2E-20260919-1",
        amount_cents=482_000,
        reason=RecallReason.FRAUD_SCAM,
        requesting_institution_id="inst-harbor-national",
        responding_institution_id="inst-northbay-cu",
        original_settled_at=NOW - timedelta(minutes=14),
        received_at=NOW,
        native=NativeEnvelope(message_id="HN-1", reason_code="FRAD", creation_time=NOW),
    )


@pytest.fixture
def peer() -> Peer:
    return Peer(
        institution_id="inst-northbay-cu",
        base_url="https://northbay.example/interlock",
        shared_secret="a-shared-secret-configured-out-of-band",
    )


class TestRoundTrip:
    def test_a_case_survives_the_wire(self) -> None:
        case = a_case()
        assert decode_case(encode_case(case, protocol_version=CURRENT_VERSION)) == case

    def test_encoding_is_byte_stable(self) -> None:
        """Two instances must produce the same bytes, or the signature depends
        on dictionary ordering rather than on content."""
        case = a_case()
        first = encode_case(case, protocol_version=CURRENT_VERSION)
        second = encode_case(case, protocol_version=CURRENT_VERSION)
        assert first == second


class TestVersionIsNegotiatedBeforeParsing:
    def test_an_unsupported_version_raises_a_version_error(self) -> None:
        """Not a validation error.

        A peer on a newer version must be told so, rather than handed a field
        error and left debugging the wrong thing for a day.
        """
        body = encode_case(a_case(), protocol_version="9.9")
        with pytest.raises(VersionNegotiationError):
            decode_case(body)

    def test_a_missing_version_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no protocol_version"):
            decode_case(b'{"case": {}}')

    def test_an_unparseable_body_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unparseable"):
            decode_case(b"not json at all")

    def test_a_body_without_a_case_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no 'case' member"):
            decode_case(b'{"protocol_version": "1.0"}')


class TestAuthentication:
    def test_a_valid_request_passes(self, peer: Peer) -> None:
        body = encode_case(a_case(), protocol_version=CURRENT_VERSION)
        stamp = NOW.isoformat()
        verify_request(
            peer, body, signature=peer.sign(body, timestamp=stamp), timestamp=stamp, now=NOW
        )

    def test_a_tampered_body_is_refused(self, peer: Peer) -> None:
        body = encode_case(a_case(), protocol_version=CURRENT_VERSION)
        stamp = NOW.isoformat()
        signature = peer.sign(body, timestamp=stamp)
        with pytest.raises(PeerAuthError):
            verify_request(
                peer,
                body.replace(b"482000", b"999900"),
                signature=signature,
                timestamp=stamp,
                now=NOW,
            )

    def test_a_wrong_secret_is_refused(self, peer: Peer) -> None:
        body = encode_case(a_case(), protocol_version=CURRENT_VERSION)
        stamp = NOW.isoformat()
        impostor = Peer(peer.institution_id, peer.base_url, "guessed-wrong")
        with pytest.raises(PeerAuthError):
            verify_request(
                peer,
                body,
                signature=impostor.sign(body, timestamp=stamp),
                timestamp=stamp,
                now=NOW,
            )

    def test_a_stale_request_is_refused(self, peer: Peer) -> None:
        """Without a replay window the signature is valid forever, and on a
        system whose job is recording who asked for what and when, a replayed
        request is a duplicate claim against a customer's account."""
        body = encode_case(a_case(), protocol_version=CURRENT_VERSION)
        stale = (NOW - REPLAY_WINDOW - timedelta(minutes=1)).isoformat()
        with pytest.raises(PeerAuthError):
            verify_request(
                peer,
                body,
                signature=peer.sign(body, timestamp=stale),
                timestamp=stale,
                now=NOW,
            )

    def test_a_replayed_signature_with_a_fresh_timestamp_is_refused(self, peer: Peer) -> None:
        """The timestamp is inside the signature, not beside it."""
        body = encode_case(a_case(), protocol_version=CURRENT_VERSION)
        original = (NOW - timedelta(minutes=1)).isoformat()
        signature = peer.sign(body, timestamp=original)
        with pytest.raises(PeerAuthError):
            verify_request(peer, body, signature=signature, timestamp=NOW.isoformat(), now=NOW)

    def test_every_failure_gives_the_same_message(self, peer: Peer) -> None:
        """A caller learning *which* check failed learns whether their key is
        valid, which is what an attacker probing a guessed secret wants."""
        body = encode_case(a_case(), protocol_version=CURRENT_VERSION)
        messages = set()

        for signature, timestamp in (
            ("0" * 64, NOW.isoformat()),
            (peer.sign(body, timestamp=NOW.isoformat()), "not-a-timestamp"),
            (
                peer.sign(body, timestamp=(NOW - timedelta(days=2)).isoformat()),
                (NOW - timedelta(days=2)).isoformat(),
            ),
        ):
            with pytest.raises(PeerAuthError) as caught:
                verify_request(peer, body, signature=signature, timestamp=timestamp, now=NOW)
            messages.add(str(caught.value))

        assert len(messages) == 1, f"failure messages differ and leak which check ran: {messages}"


class TestNetworkModeIsOptional:
    """The acceptance criterion that matters.

    Network mode is the upgrade, not the product. If removing it broke
    anything, the claim that value arrives at n=1 would be false.
    """

    def test_nothing_outside_the_network_package_imports_it(self) -> None:
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "interlock"
        offenders = []
        for path in src.rglob("*.py"):
            if "network" in path.parts:
                continue
            text = path.read_text()
            if "interlock.network" in text:
                offenders.append(str(path.relative_to(src)))

        assert not offenders, (
            "network mode must be removable without touching anything else; "
            f"imported by {offenders}"
        )

    def test_the_console_runs_with_networking_absent(self) -> None:
        """The full single-instance journey, with no peer configured."""
        import warnings

        from fastapi.testclient import TestClient

        from interlock.api.app import create_app
        from interlock.config import Settings

        warnings.filterwarnings("ignore")
        with TestClient(create_app(Settings(database_path=":memory:"))) as client:
            for path in ("/", "/rails", "/evidence", "/healthz"):
                assert client.get(path).status_code == 200
            assert client.get("/healthz").json()["status"] == "ok"
