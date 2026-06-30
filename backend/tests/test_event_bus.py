"""Unit tests for the EventBus — publish, subscribe, replay, and idempotency."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.runtime.events.bus import EventBus, MAX_DELIVERY_ATTEMPTS
from app.runtime.events.models import Event, EventType


def _make_event(
    session_id: str = "session-1",
    event_type: EventType = EventType.TASK_START,
    source: str = "test",
    seq: int = 0,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> Event:
    """Helper to create a valid test event."""
    return Event.create(
        type=event_type,
        session_id=session_id,
        source=source,
        payload={"test": True},
        causation_id=causation_id,
        correlation_id=correlation_id or session_id,
        event_id=str(uuid.uuid4()),
        seq=seq,
    )


class TestPublishSeqAssignment:
    """Test that publish assigns monotonic seq values."""

    async def test_first_event_gets_seq_1(self) -> None:
        bus = EventBus()
        event = _make_event()
        result = await bus.publish(event)
        assert result.seq == 1

    async def test_sequential_events_get_incrementing_seq(self) -> None:
        bus = EventBus()
        e1 = await bus.publish(_make_event())
        e2 = await bus.publish(_make_event())
        e3 = await bus.publish(_make_event())
        assert e1.seq == 1
        assert e2.seq == 2
        assert e3.seq == 3

    async def test_different_sessions_have_independent_seq(self) -> None:
        bus = EventBus()
        e1 = await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        e2 = await bus.publish(_make_event(session_id="s2", correlation_id="s2"))
        e3 = await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        assert e1.seq == 1
        assert e2.seq == 1  # independent counter
        assert e3.seq == 2

    async def test_concurrent_publishes_assign_unique_seq(self) -> None:
        bus = EventBus()
        events = [_make_event() for _ in range(20)]
        results = await asyncio.gather(*[bus.publish(e) for e in events])
        seqs = [r.seq for r in results]
        assert sorted(seqs) == list(range(1, 21))
        assert len(set(seqs)) == 20  # all unique


class TestSubscribeDelivery:
    """Test subscriber registration and event delivery."""

    async def test_subscriber_receives_matching_events(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("task.*", handler, "sub-1")
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        await bus.publish(_make_event(event_type=EventType.TASK_DONE))
        assert len(received) == 2

    async def test_subscriber_does_not_receive_non_matching_events(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("model.*", handler, "sub-1")
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        assert len(received) == 0

    async def test_wildcard_subscriber_receives_all_events(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*", handler, "sub-all")
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        await bus.publish(_make_event(event_type=EventType.MODEL_SELECTED))
        assert len(received) == 2

    async def test_glob_pattern_matches_suffix(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*.done", handler, "sub-done")
        await bus.publish(_make_event(event_type=EventType.TASK_DONE))
        await bus.publish(_make_event(event_type=EventType.BUILD_DONE))
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        assert len(received) == 2

    async def test_unsubscribe_removes_subscriber(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*", handler, "sub-1")
        await bus.publish(_make_event())
        assert len(received) == 1

        bus.unsubscribe("sub-1")
        await bus.publish(_make_event())
        assert len(received) == 1  # no new delivery


class TestDeliveryRetry:
    """Test at-least-once delivery with retry on failure."""

    async def test_retries_on_handler_failure(self) -> None:
        bus = EventBus()
        call_count = 0

        async def failing_handler(event: Event) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient failure")

        bus.subscribe("*", failing_handler, "flaky-sub")
        await bus.publish(_make_event())
        assert call_count == 3  # failed twice, succeeded on third

    async def test_gives_up_after_max_attempts(self) -> None:
        bus = EventBus()
        call_count = 0

        async def always_failing_handler(event: Event) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("permanent failure")

        bus.subscribe("*", always_failing_handler, "bad-sub")
        # Should not raise — failure is logged, not propagated
        await bus.publish(_make_event())
        assert call_count == MAX_DELIVERY_ATTEMPTS


class TestReplay:
    """Test event replay functionality."""

    async def test_replay_returns_all_events_for_session(self) -> None:
        bus = EventBus()
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))

        events = await bus.replay("s1")
        assert len(events) == 3
        assert [e.seq for e in events] == [1, 2, 3]

    async def test_replay_since_seq_filters_correctly(self) -> None:
        bus = EventBus()
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))

        events = await bus.replay("s1", since_seq=2)
        assert len(events) == 1
        assert events[0].seq == 3

    async def test_replay_empty_session_returns_empty_list(self) -> None:
        bus = EventBus()
        events = await bus.replay("nonexistent")
        assert events == []

    async def test_replay_does_not_return_other_sessions(self) -> None:
        bus = EventBus()
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s2", correlation_id="s2"))

        events = await bus.replay("s1")
        assert len(events) == 1
        assert events[0].session_id == "s1"


class TestIdempotency:
    """Test idempotency enforcement on (session_id, seq) pairs."""

    async def test_duplicate_event_is_noop(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*", handler, "sub-1")

        # Publish first event — gets seq 1
        event = _make_event()
        result = await bus.publish(event)
        assert result.seq == 1
        assert len(received) == 1

        # Re-publish same event with seq=1 — should be a no-op
        duplicate = _make_event(seq=1)
        result2 = await bus.publish(duplicate)
        assert result2.seq == 1
        assert len(received) == 1  # no additional delivery

    async def test_duplicate_does_not_add_to_storage(self) -> None:
        bus = EventBus()

        event = _make_event()
        await bus.publish(event)

        # Attempt duplicate with explicit seq
        duplicate = _make_event(seq=1)
        await bus.publish(duplicate)

        events = await bus.replay("session-1")
        assert len(events) == 1  # only one stored


class TestValidation:
    """Test event validation rejects incomplete events."""

    async def test_missing_source_raises(self) -> None:
        bus = EventBus()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="",  # empty
            correlation_id="s1",
            event_id="eid-1",
        )
        with pytest.raises(ValueError, match="source"):
            await bus.publish(event)

    async def test_missing_event_id_raises(self) -> None:
        bus = EventBus()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="test",
            correlation_id="s1",
            event_id="",  # empty
        )
        with pytest.raises(ValueError, match="event_id"):
            await bus.publish(event)

    async def test_missing_correlation_id_raises(self) -> None:
        bus = EventBus()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="",
            source="test",
            correlation_id="",  # empty
            event_id="eid-1",
        )
        with pytest.raises(ValueError, match="correlation_id"):
            await bus.publish(event)
