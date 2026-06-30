"""Bounded subscriber queue with streaming backpressure.

Provides per-subscriber bounded queues that:
- Drop oldest coalescible `token` events when the queue is full
- Never drop lifecycle events (*.start, *.done, *.fail, task.done, build.done, question, error)
- Block the producer up to a configurable timeout if a lifecycle event cannot fit
- Persist lifecycle events to durable storage on timeout (logged for now)

Requirements: 18.1, 18.2, 18.3, 18.4, 18.5
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections import deque
from dataclasses import dataclass, field

from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)

# Default maximum queue depth per subscriber
DEFAULT_MAX_DEPTH = 1000

# Default timeout (seconds) for blocking the producer on lifecycle events
DEFAULT_LIFECYCLE_TIMEOUT = 5.0

# Lifecycle event patterns that must never be dropped
LIFECYCLE_PATTERNS: list[str] = [
    "*.start",
    "*.done",
    "*.fail",
    "task.done",
    "build.done",
    "question",
    "error",
]

# Coalescible event types that can be dropped oldest-first under backpressure
COALESCIBLE_TYPES: set[EventType] = {EventType.TOKEN}


def is_lifecycle_event(event: Event) -> bool:
    """Check if an event is a lifecycle event that must never be dropped.

    Lifecycle events match any of the LIFECYCLE_PATTERNS against the event type value.
    """
    event_value = event.type.value
    for pattern in LIFECYCLE_PATTERNS:
        if fnmatch.fnmatch(event_value, pattern):
            return True
    return False


def is_coalescible(event: Event) -> bool:
    """Check if an event is coalescible (can be dropped oldest-first under pressure)."""
    return event.type in COALESCIBLE_TYPES


@dataclass
class BackpressureQueue:
    """Per-subscriber bounded queue with backpressure support.

    Maintains a bounded deque of events for a single subscriber. When the queue
    is full:
    - If the incoming event is coalescible or there are coalescible events to drop,
      drop oldest coalescible events to make room.
    - If the incoming event is a lifecycle event and no coalescible events can be
      dropped, block the producer up to a configurable timeout.
    - On timeout, persist the event to spillover storage (logged for now).

    Attributes:
        subscriber_id: Identifier for the owning subscriber.
        max_depth: Maximum number of events buffered.
        lifecycle_timeout: Seconds to block producer for lifecycle events.
    """

    subscriber_id: str
    max_depth: int = DEFAULT_MAX_DEPTH
    lifecycle_timeout: float = DEFAULT_LIFECYCLE_TIMEOUT
    _queue: deque[Event] = field(default_factory=deque, init=False)
    _space_available: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _spilled_events: list[Event] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # Signal that space is available initially
        self._space_available.set()

    @property
    def depth(self) -> int:
        """Current number of events in the queue."""
        return len(self._queue)

    @property
    def is_full(self) -> bool:
        """Whether the queue is at maximum depth."""
        return len(self._queue) >= self.max_depth

    @property
    def spilled_events(self) -> list[Event]:
        """Events that were persisted to spillover storage on timeout."""
        return list(self._spilled_events)

    async def enqueue(self, event: Event) -> bool:
        """Enqueue an event into the bounded queue.

        If the queue is full:
        - Drop oldest coalescible token events to make room (Req 18.2)
        - If no coalescible events and event is lifecycle: block producer (Req 18.4)
        - On timeout: persist to spillover and return False

        Args:
            event: The event to enqueue.

        Returns:
            True if the event was successfully enqueued, False if it was spilled.
        """
        if not self.is_full:
            self._queue.append(event)
            return True

        # Queue is full — try to drop oldest coalescible events
        dropped = self._drop_oldest_coalescible()

        if dropped:
            self._queue.append(event)
            self._signal_space_if_available()
            return True

        # No coalescible events to drop
        if is_lifecycle_event(event):
            # Block producer up to timeout for lifecycle events (Req 18.4)
            return await self._block_for_lifecycle(event)
        else:
            # Non-lifecycle, non-coalescible event when queue is full and no tokens to drop
            # Drop oldest coalescible event of same type, or just drop the new event
            # Per Req 18.5: keep at or below max depth
            # Since this is a non-lifecycle event and there's nothing to drop,
            # we can drop the oldest non-lifecycle event of same type
            # Actually per spec: only token events are coalescible. If a non-token,
            # non-lifecycle event arrives and queue is full with no tokens, we should
            # still try to make room by dropping oldest token. Since we already tried
            # and failed, we drop the incoming event (it's not lifecycle, so it's safe).
            logger.warning(
                "Dropping non-lifecycle event for subscriber=%s: type=%s, seq=%d "
                "(queue full, no coalescible events to drop)",
                self.subscriber_id,
                event.type.value,
                event.seq,
            )
            return False

    def dequeue(self) -> Event | None:
        """Remove and return the oldest event from the queue.

        Returns:
            The oldest event, or None if the queue is empty.
        """
        if not self._queue:
            return None
        event = self._queue.popleft()
        self._signal_space_if_available()
        return event

    def peek(self) -> Event | None:
        """Look at the oldest event without removing it."""
        if not self._queue:
            return None
        return self._queue[0]

    def drain(self) -> list[Event]:
        """Remove and return all events from the queue."""
        events = list(self._queue)
        self._queue.clear()
        self._signal_space_if_available()
        return events

    def _drop_oldest_coalescible(self) -> bool:
        """Drop the oldest coalescible (token) event from the queue.

        Returns:
            True if an event was dropped, False if no coalescible events exist.
        """
        for i, queued_event in enumerate(self._queue):
            if is_coalescible(queued_event):
                del self._queue[i]
                logger.debug(
                    "Dropped oldest coalescible event for subscriber=%s: "
                    "type=%s, seq=%d (backpressure)",
                    self.subscriber_id,
                    queued_event.type.value,
                    queued_event.seq,
                )
                return True
        return False

    async def _block_for_lifecycle(self, event: Event) -> bool:
        """Block the producer for a lifecycle event until space is available or timeout.

        If timeout occurs, persist the event to spillover storage.

        Args:
            event: The lifecycle event that needs to be enqueued.

        Returns:
            True if the event was eventually enqueued, False if it was spilled.
        """
        self._space_available.clear()

        try:
            await asyncio.wait_for(
                self._space_available.wait(),
                timeout=self.lifecycle_timeout,
            )
            # Space became available — enqueue the event
            self._queue.append(event)
            return True
        except asyncio.TimeoutError:
            # Timeout — persist to spillover storage (Req 18.4)
            self._persist_spillover(event)
            return False

    def _persist_spillover(self, event: Event) -> None:
        """Persist a lifecycle event to spillover storage on timeout.

        Currently logs and stores in an in-memory list. In production this would
        write to durable storage (e.g., the relational store).
        """
        self._spilled_events.append(event)
        logger.warning(
            "Lifecycle event spilled to durable storage for subscriber=%s: "
            "type=%s, seq=%d, session=%s (producer blocked for %.1fs)",
            self.subscriber_id,
            event.type.value,
            event.seq,
            event.session_id,
            self.lifecycle_timeout,
        )

    def _signal_space_if_available(self) -> None:
        """Signal that space is available if the queue is below max depth."""
        if not self.is_full:
            self._space_available.set()

    def clear(self) -> None:
        """Clear all events from the queue."""
        self._queue.clear()
        self._space_available.set()
