"""In-process EventBus — single source of truth for all runtime events.

Provides typed publish/subscribe with:
- Per-session monotonic seq assignment (starting at 1)
- Glob-style subscriber pattern matching on EventType values
- At-least-once delivery with retry (up to 5 attempts per subscriber)
- Idempotency enforcement on (session_id, seq) pairs
- In-memory event storage for replay
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)

# Type alias for subscriber handler functions
SubscriberHandler = Callable[[Event], Awaitable[None]]

MAX_DELIVERY_ATTEMPTS = 5


@dataclass
class Subscriber:
    """A registered event subscriber with a glob pattern and handler."""

    pattern: str
    handler: SubscriberHandler
    subscriber_id: str


class EventBus:
    """In-process typed event bus — the architectural spine of the Forge Runtime.

    All runtime occurrences are published as Events through this bus. The bus assigns
    monotonic per-session seq values, enforces idempotency, delivers to subscribers
    with at-least-once semantics, and stores events for replay.
    """

    def __init__(self) -> None:
        # Per-session seq counters and locks
        self._seq_counters: dict[str, int] = defaultdict(int)
        self._seq_locks: dict[str, asyncio.Lock] = {}

        # Event storage keyed by correlation_id (session_id)
        self._events: dict[str, list[Event]] = defaultdict(list)

        # Idempotency set: tracks (session_id, seq) pairs already published
        self._published: set[tuple[str, int]] = set()

        # Registered subscribers
        self._subscribers: list[Subscriber] = []

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create the asyncio.Lock for a given session."""
        if session_id not in self._seq_locks:
            self._seq_locks[session_id] = asyncio.Lock()
        return self._seq_locks[session_id]

    async def publish(self, event: Event) -> Event:
        """Publish an event to the bus.

        Assigns a monotonic seq under lock, enforces idempotency, stores the event,
        and delivers to all matching subscribers with at-least-once retry.

        Args:
            event: The event to publish. If seq is 0, a new seq will be assigned.
                   If seq is already set, idempotency check is performed.

        Returns:
            The event with seq assigned.

        Raises:
            ValueError: If the event is missing required fields.
        """
        self._validate_event(event)

        session_id = event.correlation_id or event.session_id

        lock = self._get_lock(session_id)
        async with lock:
            # If event already has a seq > 0, check idempotency
            if event.seq > 0:
                key = (session_id, event.seq)
                if key in self._published:
                    # Duplicate — no-op per idempotency contract
                    return event
                self._published.add(key)
                self._events[session_id].append(event)
            else:
                # Assign next seq
                self._seq_counters[session_id] += 1
                new_seq = self._seq_counters[session_id]

                # Create new event with assigned seq
                event = Event(
                    schema_version=event.schema_version,
                    seq=new_seq,
                    session_id=event.session_id,
                    type=event.type,
                    timestamp=event.timestamp,
                    source=event.source,
                    payload=event.payload,
                    causation_id=event.causation_id,
                    correlation_id=event.correlation_id,
                    event_id=event.event_id,
                )

                key = (session_id, new_seq)
                self._published.add(key)
                self._events[session_id].append(event)

        # Deliver to matching subscribers (outside the lock)
        await self._deliver(event)

        return event

    def _validate_event(self, event: Event) -> None:
        """Validate that an event has all required fields.

        Raises:
            ValueError: If any required field is missing or empty.
        """
        missing: list[str] = []

        if not event.type:
            missing.append("type")
        if not (event.correlation_id or event.session_id):
            missing.append("correlation_id")
        if not event.source:
            missing.append("source")
        if not event.event_id:
            missing.append("event_id")
        if event.timestamp is None:
            missing.append("timestamp")

        if missing:
            raise ValueError(
                f"Event missing required fields: {', '.join(missing)}"
            )

    def subscribe(self, pattern: str, handler: SubscriberHandler, subscriber_id: str) -> None:
        """Register a subscriber with a glob pattern.

        The pattern is matched against EventType values (e.g., "capability.*",
        "*.done", "task.start").

        Args:
            pattern: Glob-style pattern to match against event type values.
            handler: Async callable invoked for each matching event.
            subscriber_id: Unique identifier for the subscriber.
        """
        self._subscribers.append(
            Subscriber(pattern=pattern, handler=handler, subscriber_id=subscriber_id)
        )

    def unsubscribe(self, subscriber_id: str) -> None:
        """Remove a subscriber by its ID.

        Args:
            subscriber_id: The subscriber to remove.
        """
        self._subscribers = [
            s for s in self._subscribers if s.subscriber_id != subscriber_id
        ]

    async def replay(
        self, correlation_id: str, since_seq: int = 0
    ) -> list[Event]:
        """Replay stored events for a given correlation_id.

        Returns all events with seq > since_seq, ordered by seq.

        Args:
            correlation_id: The session/correlation ID to replay events for.
            since_seq: Only return events with seq strictly greater than this value.

        Returns:
            List of events ordered by seq.
        """
        events = self._events.get(correlation_id, [])
        return [e for e in events if e.seq > since_seq]

    def _matches(self, pattern: str, event_type: EventType) -> bool:
        """Check if a glob pattern matches an event type value.

        Args:
            pattern: Glob-style pattern (e.g., "capability.*", "*.done").
            event_type: The EventType to match against.

        Returns:
            True if the pattern matches the event type's value.
        """
        return fnmatch.fnmatch(event_type.value, pattern)

    async def _deliver(self, event: Event) -> None:
        """Deliver an event to all matching subscribers with at-least-once retry.

        Retries up to MAX_DELIVERY_ATTEMPTS times per subscriber on failure.
        """
        for subscriber in self._subscribers:
            if self._matches(subscriber.pattern, event.type):
                await self._deliver_to_subscriber(event, subscriber)

    async def _deliver_to_subscriber(
        self, event: Event, subscriber: Subscriber
    ) -> None:
        """Deliver an event to a single subscriber with retry.

        Args:
            event: The event to deliver.
            subscriber: The target subscriber.
        """
        session_id = event.correlation_id or event.session_id

        for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
            try:
                await subscriber.handler(event)
                return  # Success
            except Exception:
                if attempt == MAX_DELIVERY_ATTEMPTS:
                    logger.error(
                        "Delivery failed after %d attempts: subscriber=%s, "
                        "session_id=%s, seq=%d",
                        MAX_DELIVERY_ATTEMPTS,
                        subscriber.subscriber_id,
                        session_id,
                        event.seq,
                    )
                else:
                    logger.warning(
                        "Delivery attempt %d/%d failed: subscriber=%s, "
                        "session_id=%s, seq=%d",
                        attempt,
                        MAX_DELIVERY_ATTEMPTS,
                        subscriber.subscriber_id,
                        session_id,
                        event.seq,
                    )

    def get_seq(self, session_id: str) -> int:
        """Get the current seq counter for a session.

        Args:
            session_id: The session to query.

        Returns:
            The current highest seq for the session, or 0 if no events published.
        """
        return self._seq_counters.get(session_id, 0)

    def clear_session(self, session_id: str) -> None:
        """Clear all stored state for a session.

        Removes events, seq counter, and published keys for the given session.

        Args:
            session_id: The session to clear.
        """
        self._events.pop(session_id, None)
        self._seq_counters.pop(session_id, None)
        self._seq_locks.pop(session_id, None)
        self._published = {
            (sid, seq) for sid, seq in self._published if sid != session_id
        }
