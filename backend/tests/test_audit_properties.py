"""Property-based tests for audit replay fidelity using Hypothesis.

**Validates: Requirements 19.3, 19.4**

Property 6: Audit replay fidelity — replaying audit_log ordered by seq
    reconstructs a faithful projection: persisted events are field-for-field
    equal to the originals, in the same order, with no events lost or duplicated.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.runtime.events.models import Event, EventType
from app.runtime.audit import AuditTrail


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

event_types = st.sampled_from(list(EventType))

# Strategy for generating arbitrary payload dicts
payload_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1000, max_value=1000),
        st.text(min_size=0, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N", "P", "S"))),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(
            st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L",))),
            children,
            max_size=5,
        ),
    ),
    max_leaves=10,
)

payloads = st.dictionaries(
    st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("L",))),
    payload_values,
    min_size=0,
    max_size=5,
)


def make_event(
    session_id: str,
    seq: int,
    event_type: EventType,
    source: str,
    payload: dict,
    event_id: str | None = None,
) -> Event:
    """Create a valid event with an assigned seq (as if published by EventBus)."""
    return Event.create(
        type=event_type,
        session_id=session_id,
        source=source,
        payload=payload,
        correlation_id=session_id,
        event_id=event_id or str(uuid.uuid4()),
        seq=seq,
    )


# Strategy for source component names
sources = st.sampled_from([
    "workflow_engine",
    "model_router",
    "verification_pipeline",
    "task_dispatcher",
    "capability_registry",
    "health_monitor",
    "policy_engine",
    "property-test",
])


# ---------------------------------------------------------------------------
# Property 6: Audit replay fidelity
# ---------------------------------------------------------------------------


class TestAuditReplayFidelityProperty:
    """Property 6: Audit replay fidelity — replaying audit_log ordered by seq
    reconstructs a faithful projection whose persisted fields are field-for-field
    equal to the original events.

    **Validates: Requirements 19.3, 19.4**
    """

    @given(
        num_events=st.integers(min_value=1, max_value=30),
        data=st.data(),
    )
    @settings(max_examples=100, deadline=5000)
    async def test_replay_reconstructs_original_sequence(
        self,
        num_events: int,
        data: st.DataObject,
    ) -> None:
        """Generate arbitrary sequences of events with varying types and payloads,
        persist them through the AuditTrail, then replay ordered by seq. Verify
        the replayed sequence is identical to the original: same events in the
        same order, field-for-field equal.
        """
        audit = AuditTrail()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        # Generate and persist a sequence of events
        original_events: list[Event] = []
        for seq_num in range(1, num_events + 1):
            event_type = data.draw(event_types, label=f"type_{seq_num}")
            source = data.draw(sources, label=f"source_{seq_num}")
            payload = data.draw(payloads, label=f"payload_{seq_num}")

            event = make_event(
                session_id=session_id,
                seq=seq_num,
                event_type=event_type,
                source=source,
                payload=payload,
            )
            await audit.handle_event(event)
            original_events.append(event)

        # Replay: retrieve all events ordered by seq
        replayed_events = audit.get_events(session_id)

        # Property: no events lost
        assert len(replayed_events) == len(original_events), (
            f"Event count mismatch: original={len(original_events)}, "
            f"replayed={len(replayed_events)}"
        )

        # Property: field-for-field equality in seq order
        for i, (original, replayed) in enumerate(
            zip(original_events, replayed_events)
        ):
            assert replayed.seq == original.seq, (
                f"Event {i}: seq mismatch: original={original.seq}, "
                f"replayed={replayed.seq}"
            )
            assert replayed.type == original.type, (
                f"Event {i} (seq={original.seq}): type mismatch: "
                f"original={original.type}, replayed={replayed.type}"
            )
            assert replayed.payload == original.payload, (
                f"Event {i} (seq={original.seq}): payload mismatch: "
                f"original={original.payload}, replayed={replayed.payload}"
            )
            assert replayed.source == original.source, (
                f"Event {i} (seq={original.seq}): source mismatch: "
                f"original={original.source}, replayed={replayed.source}"
            )
            assert replayed.session_id == original.session_id, (
                f"Event {i} (seq={original.seq}): session_id mismatch: "
                f"original={original.session_id}, replayed={replayed.session_id}"
            )
            assert replayed.event_id == original.event_id, (
                f"Event {i} (seq={original.seq}): event_id mismatch: "
                f"original={original.event_id}, replayed={replayed.event_id}"
            )
            assert replayed.correlation_id == original.correlation_id, (
                f"Event {i} (seq={original.seq}): correlation_id mismatch: "
                f"original={original.correlation_id}, "
                f"replayed={replayed.correlation_id}"
            )

    @given(
        num_events=st.integers(min_value=2, max_value=25),
        data=st.data(),
    )
    @settings(max_examples=80, deadline=5000)
    async def test_out_of_order_persistence_replays_in_seq_order(
        self,
        num_events: int,
        data: st.DataObject,
    ) -> None:
        """Events persisted out of seq order are still replayed in correct
        seq order. This verifies the audit trail's ordering guarantee.
        """
        audit = AuditTrail()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        # Generate events with sequential seq values
        events: list[Event] = []
        for seq_num in range(1, num_events + 1):
            event_type = data.draw(event_types, label=f"type_{seq_num}")
            source = data.draw(sources, label=f"source_{seq_num}")
            payload = data.draw(payloads, label=f"payload_{seq_num}")

            event = make_event(
                session_id=session_id,
                seq=seq_num,
                event_type=event_type,
                source=source,
                payload=payload,
            )
            events.append(event)

        # Shuffle the order of persistence using Hypothesis
        shuffled_indices = data.draw(
            st.permutations(list(range(len(events)))),
            label="persistence_order",
        )

        # Persist in shuffled order
        for idx in shuffled_indices:
            await audit.handle_event(events[idx])

        # Replay should still return in seq order
        replayed_events = audit.get_events(session_id)

        assert len(replayed_events) == num_events, (
            f"Event count mismatch after out-of-order persist: "
            f"expected={num_events}, got={len(replayed_events)}"
        )

        # Verify seq ordering is correct (ascending)
        for i in range(1, len(replayed_events)):
            assert replayed_events[i].seq > replayed_events[i - 1].seq, (
                f"Replay not in seq order at index {i}: "
                f"seq[{i-1}]={replayed_events[i-1].seq}, "
                f"seq[{i}]={replayed_events[i].seq}"
            )

        # Verify field-for-field match with original events (sorted by seq)
        sorted_originals = sorted(events, key=lambda e: e.seq)
        for original, replayed in zip(sorted_originals, replayed_events):
            assert replayed.event_id == original.event_id
            assert replayed.type == original.type
            assert replayed.payload == original.payload
            assert replayed.source == original.source
            assert replayed.seq == original.seq

    @given(
        num_events=st.integers(min_value=1, max_value=20),
        data=st.data(),
    )
    @settings(max_examples=80, deadline=5000)
    async def test_no_events_lost_or_duplicated(
        self,
        num_events: int,
        data: st.DataObject,
    ) -> None:
        """Verify that after persisting N unique events, exactly N events are
        retrievable — no events lost, no phantom duplicates created.
        """
        audit = AuditTrail()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        event_ids: list[str] = []
        for seq_num in range(1, num_events + 1):
            event_type = data.draw(event_types, label=f"type_{seq_num}")
            payload = data.draw(payloads, label=f"payload_{seq_num}")
            event_id = str(uuid.uuid4())
            event_ids.append(event_id)

            event = make_event(
                session_id=session_id,
                seq=seq_num,
                event_type=event_type,
                source="property-test",
                payload=payload,
                event_id=event_id,
            )
            await audit.handle_event(event)

        replayed = audit.get_events(session_id)

        # No events lost
        assert len(replayed) == num_events, (
            f"Events lost: expected {num_events}, got {len(replayed)}"
        )

        # No duplicates — all event_ids are unique in replay
        replayed_ids = [e.event_id for e in replayed]
        assert len(replayed_ids) == len(set(replayed_ids)), (
            f"Duplicate event_ids in replay: {replayed_ids}"
        )

        # All original event_ids are present
        assert set(replayed_ids) == set(event_ids), (
            f"Event ID set mismatch: "
            f"missing={set(event_ids) - set(replayed_ids)}, "
            f"extra={set(replayed_ids) - set(event_ids)}"
        )

    @given(
        num_events=st.integers(min_value=1, max_value=15),
        data=st.data(),
    )
    @settings(max_examples=60, deadline=5000)
    async def test_duplicate_persistence_is_idempotent(
        self,
        num_events: int,
        data: st.DataObject,
    ) -> None:
        """Persisting the same event twice (same session_id, seq) must not
        create duplicates in the audit log. The second persist is a no-op
        per Requirement 19.6.
        """
        audit = AuditTrail()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        events: list[Event] = []
        for seq_num in range(1, num_events + 1):
            event_type = data.draw(event_types, label=f"type_{seq_num}")
            payload = data.draw(payloads, label=f"payload_{seq_num}")

            event = make_event(
                session_id=session_id,
                seq=seq_num,
                event_type=event_type,
                source="property-test",
                payload=payload,
            )
            events.append(event)

        # Persist all events once
        for event in events:
            await audit.handle_event(event)

        # Pick random events to re-persist (duplicates)
        num_duplicates = data.draw(
            st.integers(min_value=1, max_value=num_events),
            label="num_duplicates",
        )
        duplicate_indices = data.draw(
            st.lists(
                st.integers(min_value=0, max_value=num_events - 1),
                min_size=num_duplicates,
                max_size=num_duplicates,
            ),
            label="duplicate_indices",
        )

        # Re-persist duplicates — should be idempotent
        for idx in duplicate_indices:
            await audit.handle_event(events[idx])

        # Replay should still have exactly num_events entries
        replayed = audit.get_events(session_id)
        assert len(replayed) == num_events, (
            f"Duplicate persistence created extra entries: "
            f"expected={num_events}, got={len(replayed)}"
        )
