"""Property-based tests for event ordering and causality using Hypothesis.

**Validates: Requirements 17.3, 17.4, 17.5**

Property 4: Event ordering — for all events sharing a correlation_id,
    seq values are unique and strictly increasing.

Property 5: Causality closure — for every non-root event, causation_id
    references an event with smaller seq in the same session.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import given, settings, strategies as st

from app.runtime.events.bus import EventBus
from app.runtime.events.models import Event, EventType


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Use a fixed subset of event types for generation (all are valid for publishing)
event_types = st.sampled_from(list(EventType))


def make_event(
    session_id: str,
    correlation_id: str,
    event_type: EventType,
    causation_id: str | None = None,
) -> Event:
    """Create a valid event ready for publishing (seq=0 means bus assigns seq)."""
    return Event.create(
        type=event_type,
        session_id=session_id,
        source="property-test",
        payload={"generated": True},
        causation_id=causation_id,
        correlation_id=correlation_id,
        event_id=str(uuid.uuid4()),
        seq=0,
    )


# Strategy: generate a list of 1–30 event types for a single session
event_type_lists = st.lists(event_types, min_size=1, max_size=30)


# ---------------------------------------------------------------------------
# Property 4: Event ordering
# ---------------------------------------------------------------------------


class TestEventOrderingProperty:
    """Property 4: Event ordering — for all events sharing a correlation_id,
    seq values are unique and strictly increasing.

    **Validates: Requirements 17.3**
    """

    @given(type_list=event_type_lists)
    @settings(max_examples=100, deadline=5000)
    async def test_seq_values_unique_and_strictly_increasing(
        self, type_list: list[EventType]
    ) -> None:
        """For any sequence of events published to the same session/correlation_id,
        seq values must be unique and form a strictly increasing sequence starting at 1.
        """
        bus = EventBus()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        published_events: list[Event] = []
        for event_type in type_list:
            event = make_event(
                session_id=session_id,
                correlation_id=session_id,
                event_type=event_type,
            )
            result = await bus.publish(event)
            published_events.append(result)

        # Extract seq values
        seq_values = [e.seq for e in published_events]

        # All seq values must be unique
        assert len(seq_values) == len(set(seq_values)), (
            f"Duplicate seq values found: {seq_values}"
        )

        # Seq values must be strictly increasing (each > previous)
        for i in range(1, len(seq_values)):
            assert seq_values[i] > seq_values[i - 1], (
                f"Seq not strictly increasing at index {i}: "
                f"{seq_values[i - 1]} -> {seq_values[i]}"
            )

        # First seq must be 1
        assert seq_values[0] == 1, f"First seq should be 1, got {seq_values[0]}"

        # Seq values must increment by exactly 1 (contiguous)
        expected = list(range(1, len(seq_values) + 1))
        assert seq_values == expected, (
            f"Seq values not contiguous: expected {expected}, got {seq_values}"
        )

    @given(
        type_list_a=st.lists(event_types, min_size=1, max_size=15),
        type_list_b=st.lists(event_types, min_size=1, max_size=15),
    )
    @settings(max_examples=50, deadline=5000)
    async def test_independent_sessions_have_independent_seq(
        self,
        type_list_a: list[EventType],
        type_list_b: list[EventType],
    ) -> None:
        """Events in different sessions must have independent seq counters.
        Publishing to session A must not affect session B's seq values.
        """
        bus = EventBus()
        session_a = f"session-a-{uuid.uuid4().hex[:8]}"
        session_b = f"session-b-{uuid.uuid4().hex[:8]}"

        results_a: list[Event] = []
        results_b: list[Event] = []

        # Interleave publishes between sessions
        idx_a = 0
        idx_b = 0
        while idx_a < len(type_list_a) or idx_b < len(type_list_b):
            if idx_a < len(type_list_a):
                event = make_event(
                    session_id=session_a,
                    correlation_id=session_a,
                    event_type=type_list_a[idx_a],
                )
                result = await bus.publish(event)
                results_a.append(result)
                idx_a += 1

            if idx_b < len(type_list_b):
                event = make_event(
                    session_id=session_b,
                    correlation_id=session_b,
                    event_type=type_list_b[idx_b],
                )
                result = await bus.publish(event)
                results_b.append(result)
                idx_b += 1

        # Each session's seq values are independently 1, 2, 3, ...
        seqs_a = [e.seq for e in results_a]
        seqs_b = [e.seq for e in results_b]

        assert seqs_a == list(range(1, len(type_list_a) + 1)), (
            f"Session A seq not independent: {seqs_a}"
        )
        assert seqs_b == list(range(1, len(type_list_b) + 1)), (
            f"Session B seq not independent: {seqs_b}"
        )


# ---------------------------------------------------------------------------
# Property 5: Causality closure
# ---------------------------------------------------------------------------


class TestCausalityClosureProperty:
    """Property 5: Causality closure — for every non-root event, causation_id
    references an event with smaller seq in the same session.

    **Validates: Requirements 17.4, 17.5**
    """

    @given(
        num_events=st.integers(min_value=2, max_value=20),
        data=st.data(),
    )
    @settings(max_examples=100, deadline=5000)
    async def test_causation_always_references_prior_event(
        self,
        num_events: int,
        data: st.DataObject,
    ) -> None:
        """Generate a chain of events where non-root events reference a prior event's
        event_id as their causation_id. After publishing, verify every non-root event's
        causation_id points to an event with smaller seq in the same session.
        """
        bus = EventBus()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        published_events: list[Event] = []

        # First event is always a root event (no causation_id)
        root_type = data.draw(event_types, label="root_event_type")
        root_event = make_event(
            session_id=session_id,
            correlation_id=session_id,
            event_type=root_type,
            causation_id=None,
        )
        root_result = await bus.publish(root_event)
        published_events.append(root_result)

        # Subsequent events: each one picks a random prior event as its cause
        for i in range(1, num_events):
            event_type = data.draw(event_types, label=f"event_type_{i}")

            # Pick a random prior event index to be the cause
            cause_index = data.draw(
                st.integers(min_value=0, max_value=len(published_events) - 1),
                label=f"cause_index_{i}",
            )
            cause_event = published_events[cause_index]

            event = make_event(
                session_id=session_id,
                correlation_id=session_id,
                event_type=event_type,
                causation_id=cause_event.event_id,
            )
            result = await bus.publish(event)
            published_events.append(result)

        # Build lookup: event_id -> seq
        event_id_to_seq: dict[str, int] = {
            e.event_id: e.seq for e in published_events
        }

        # Verify causality closure property
        for event in published_events:
            if event.causation_id is None:
                # Root event — Requirement 17.5: no prior cause, valid
                continue

            # Requirement 17.4: causation_id must reference existing event
            assert event.causation_id in event_id_to_seq, (
                f"Event seq={event.seq} has causation_id='{event.causation_id}' "
                f"which does not reference any published event"
            )

            # The referenced event must have a smaller seq
            cause_seq = event_id_to_seq[event.causation_id]
            assert cause_seq < event.seq, (
                f"Event seq={event.seq} has causation_id referencing event with "
                f"seq={cause_seq}, which is not smaller"
            )

    @given(
        num_roots=st.integers(min_value=1, max_value=10),
        num_caused=st.integers(min_value=0, max_value=15),
        data=st.data(),
    )
    @settings(max_examples=80, deadline=5000)
    async def test_mixed_root_and_caused_events_maintain_closure(
        self,
        num_roots: int,
        num_caused: int,
        data: st.DataObject,
    ) -> None:
        """Generate a mix of root events (causation_id=None) and caused events.
        Verify that all non-root events satisfy the causality closure property,
        and root events correctly have causation_id=None.
        """
        bus = EventBus()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        published_events: list[Event] = []

        # Publish root events first
        for _ in range(num_roots):
            event_type = data.draw(event_types, label="root_type")
            event = make_event(
                session_id=session_id,
                correlation_id=session_id,
                event_type=event_type,
                causation_id=None,
            )
            result = await bus.publish(event)
            published_events.append(result)

        # Publish caused events (each references a random prior event)
        for i in range(num_caused):
            event_type = data.draw(event_types, label=f"caused_type_{i}")
            cause_index = data.draw(
                st.integers(min_value=0, max_value=len(published_events) - 1),
                label=f"cause_idx_{i}",
            )
            cause_event = published_events[cause_index]

            event = make_event(
                session_id=session_id,
                correlation_id=session_id,
                event_type=event_type,
                causation_id=cause_event.event_id,
            )
            result = await bus.publish(event)
            published_events.append(result)

        # Build lookup: event_id -> seq
        event_id_to_seq: dict[str, int] = {
            e.event_id: e.seq for e in published_events
        }

        # Verify properties for all events
        for event in published_events:
            if event.causation_id is None:
                # Root event — Requirement 17.5
                continue

            # Causation_id must reference an existing event (Req 17.4)
            assert event.causation_id in event_id_to_seq, (
                f"Event seq={event.seq} causation_id='{event.causation_id}' "
                f"not found in published events"
            )

            # Referenced event must have smaller seq (Req 17.4)
            cause_seq = event_id_to_seq[event.causation_id]
            assert cause_seq < event.seq, (
                f"Event seq={event.seq} references cause with seq={cause_seq} "
                f"(not smaller)"
            )

    @given(event_type=event_types)
    @settings(max_examples=50, deadline=5000)
    async def test_root_events_have_no_causation_id(
        self, event_type: EventType
    ) -> None:
        """Root events (first in session or explicitly no cause) must have
        causation_id=None per Requirement 17.5.
        """
        bus = EventBus()
        session_id = f"session-{uuid.uuid4().hex[:8]}"

        event = make_event(
            session_id=session_id,
            correlation_id=session_id,
            event_type=event_type,
            causation_id=None,
        )
        result = await bus.publish(event)

        assert result.causation_id is None, (
            f"Root event should have causation_id=None, got '{result.causation_id}'"
        )
        assert result.seq == 1, f"First event should have seq=1, got {result.seq}"
