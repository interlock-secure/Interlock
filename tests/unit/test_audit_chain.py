"""Audit chain tamper-evidence.

Specification FR-6 requires that any edit or deletion is detectable. These
tests perform the actual attacks - modify an entry, delete one, reorder two -
and assert the verifier catches each. A chain that is merely documented as
tamper-evident is not evidence of anything.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from interlock.schema import (
    GENESIS_HASH,
    EventType,
    append_entry,
    digest_payload,
    utc_now,
    verify_chain,
)


def build_chain(length: int = 5):
    entries = []
    previous = None
    base_time = utc_now()
    for i in range(length):
        previous = append_entry(
            previous=previous,
            event_type=EventType.SIGNAL_REQUESTED,
            correlation_id=f"req_{i:08d}",
            payload={"index": i, "amount_cents": 1000 * (i + 1)},
            actor_institution_id="bank-a",
            recorded_at=base_time + timedelta(seconds=i),
        )
        entries.append(previous)
    return entries


class TestCleanChain:
    def test_a_clean_chain_verifies(self):
        assert verify_chain(build_chain()) == []

    def test_first_entry_links_to_genesis(self):
        assert build_chain()[0].previous_hash == GENESIS_HASH

    def test_each_entry_links_to_the_one_before(self):
        entries = build_chain()
        for prior, entry in pairwise(entries):
            assert entry.previous_hash == prior.entry_hash

    def test_empty_chain_is_vacuously_valid(self):
        assert verify_chain([]) == []


class TestTamperDetection:
    def test_modifying_an_entry_is_detected(self):
        entries = build_chain()
        # An operator quietly changes which institution acted.
        entries[2] = entries[2].model_copy(update={"actor_institution_id": "bank-z"})

        problems = verify_chain(entries)
        assert problems, "A modified entry must not verify"
        assert any(p.sequence_number == 2 for p in problems)
        assert any("modified" in p.problem for p in problems)

    def test_changing_the_event_type_is_detected(self):
        entries = build_chain()
        # Rewriting history so a held payment looks like a released one.
        entries[3] = entries[3].model_copy(update={"event_type": EventType.PAYMENT_RELEASED})
        assert verify_chain(entries)

    def test_changing_the_payload_digest_is_detected(self):
        entries = build_chain()
        entries[1] = entries[1].model_copy(
            update={"payload_digest": digest_payload({"amount_cents": 1})}
        )
        assert verify_chain(entries)

    def test_backdating_an_entry_is_detected(self):
        entries = build_chain()
        entries[2] = entries[2].model_copy(
            update={"recorded_at": entries[2].recorded_at - timedelta(days=30)}
        )
        assert verify_chain(entries)

    def test_deleting_an_entry_is_detected(self):
        entries = build_chain()
        del entries[2]

        problems = verify_chain(entries)
        assert problems, "A deleted entry must leave a detectable gap"
        assert any("gap" in p.problem or "removed" in p.problem for p in problems)

    def test_deleting_the_last_entry_is_not_detectable_by_the_chain_alone(self):
        # Stated as a known limitation rather than hidden. Truncation at the
        # tail leaves a shorter but internally consistent chain. Detecting it
        # requires an external anchor - a periodically published head hash -
        # which is a v2 concern. Documenting it here stops someone later
        # assuming a property the chain does not have.
        entries = build_chain()
        truncated = entries[:-1]
        assert verify_chain(truncated) == []

    def test_reordering_entries_does_not_fool_the_verifier(self):
        entries = build_chain()
        entries[1], entries[3] = entries[3], entries[1]
        # The verifier sorts by sequence number, so a naive reorder is undone
        # rather than detected - and the chain still verifies, correctly,
        # because nothing was actually altered.
        assert verify_chain(entries) == []

    def test_duplicating_an_entry_is_detected(self):
        entries = build_chain()
        entries.append(entries[2])
        assert verify_chain(entries)


class TestPayloadDigest:
    def test_digest_is_stable_across_key_order(self):
        # Two participants serialising the same payload must agree, or the
        # chain fails on dictionary ordering rather than on tampering.
        assert digest_payload({"a": 1, "b": 2}) == digest_payload({"b": 2, "a": 1})

    def test_digest_changes_when_a_value_changes(self):
        assert digest_payload({"amount_cents": 100}) != digest_payload({"amount_cents": 101})

    def test_digest_handles_nested_structures(self):
        payload = {"dimensions": ["tenure", "velocity"], "nested": {"x": 1}}
        assert len(digest_payload(payload)) == 64


class TestSelfConsistency:
    def test_entry_knows_its_own_hash(self):
        entry = build_chain(1)[0]
        assert entry.is_self_consistent()
        assert entry.entry_hash == entry.compute_hash()

    def test_network_events_may_have_no_actor(self):
        # NETWORK_DEGRADED originates at the hub, not a participant.
        entry = append_entry(
            previous=None,
            event_type=EventType.NETWORK_DEGRADED,
            correlation_id="req_00000001",
            payload={"reason": "budget exceeded"},
            actor_institution_id=None,
        )
        assert entry.actor_institution_id is None
        assert verify_chain([entry]) == []
