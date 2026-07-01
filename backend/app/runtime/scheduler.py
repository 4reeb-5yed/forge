"""Session Scheduler - Concurrent build execution management.

Manages multiple session executions in parallel with configurable concurrency limits.
This enables Forge to handle multiple builds simultaneously.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SchedulerStatus(Enum):
    """Status of the scheduler."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class SessionStatus(Enum):
    """Status of a session in the scheduler."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueuedSession:
    """A session queued for execution."""
    session_id: str
    session_data: dict[str, Any]
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: SessionStatus = SessionStatus.QUEUED
    task: asyncio.Task | None = None
    priority: int = 0  # Higher = more priority


class SessionScheduler:
    """Scheduler for managing concurrent session executions.

    This enables Forge to run multiple builds in parallel, with configurable
    limits on concurrent sessions. Sessions are queued and executed based on
    priority and availability of execution slots.
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        event_emitter: Any | None = None,
    ) -> None:
        """Initialize the session scheduler.

        Args:
            max_concurrent: Maximum number of concurrent session executions.
            event_emitter: Optional event bus publisher.
        """
        self._max_concurrent = max_concurrent
        self._event_emitter = event_emitter
        self._queue: asyncio.PriorityQueue[QueuedSession] = asyncio.PriorityQueue()
        self._running: dict[str, QueuedSession] = {}  # session_id -> QueuedSession
        self._completed: dict[str, QueuedSession] = {}  # session_id -> QueuedSession
        self._status = SchedulerStatus.IDLE
        self._scheduler_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._session_handlers: dict[str, Any] = {}  # session_id -> handler function

    def update_event_emitter(self, event_emitter: Any) -> None:
        """Update the event emitter after initialization.

        Args:
            event_emitter: Event bus publisher function.
        """
        self._event_emitter = event_emitter
        logger.debug("SessionScheduler event emitter updated")

    @property
    def max_concurrent(self) -> int:
        """Maximum concurrent sessions allowed."""
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        """Number of currently running sessions."""
        return len(self._running)

    @property
    def queued_count(self) -> int:
        """Number of sessions in the queue."""
        return self._queue.qsize()

    @property
    def status(self) -> SchedulerStatus:
        """Current scheduler status."""
        return self._status

    def register_handler(self, session_id: str, handler: Any) -> None:
        """Register an execution handler for a session.

        Args:
            session_id: The session ID.
            handler: Async function that executes the session.
        """
        self._session_handlers[session_id] = handler

    def unregister_handler(self, session_id: str) -> None:
        """Unregister an execution handler for a session.

        Args:
            session_id: The session ID.
        """
        self._session_handlers.pop(session_id, None)

    async def enqueue(
        self,
        session_id: str,
        session_data: dict[str, Any],
        priority: int = 0,
    ) -> None:
        """Add a session to the execution queue.

        Args:
            session_id: The session ID.
            session_data: Initial state for the session.
            priority: Session priority (higher = more important).
        """
        queued = QueuedSession(
            session_id=session_id,
            session_data=session_data,
            priority=priority,
        )

        # Negative priority for max-heap behavior (lower value = higher priority)
        await self._queue.put((-priority, queued))

        logger.info(
            "Session %s enqueued (priority=%d, queue_size=%d)",
            session_id, priority, self._queue.qsize()
        )

        # Emit event
        if self._event_emitter:
            from app.runtime.events.models import Event, EventType
            event = Event.create(
                source="scheduler",
                type=EventType.SCHEDULER_SESSION_QUEUED,
                session_id=session_id,
                payload={"priority": priority, "queue_size": self._queue.qsize()},
            )
            await self._event_emitter(event)

        # Start scheduler if not running
        if self._status == SchedulerStatus.IDLE:
            await self.start()

    async def start(self) -> None:
        """Start the scheduler."""
        async with self._lock:
            if self._status == SchedulerStatus.RUNNING:
                return

            self._status = SchedulerStatus.RUNNING
            self._scheduler_task = asyncio.create_task(self._run_scheduler())
            logger.info("Session scheduler started (max_concurrent=%d)", self._max_concurrent)

            # Emit event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id="system",
                    payload={"max_concurrent": self._max_concurrent},
                )
                await self._event_emitter(event)

    async def stop(self) -> None:
        """Stop the scheduler and cancel all running sessions."""
        async with self._lock:
            self._status = SchedulerStatus.STOPPED

            # Cancel scheduler task
            if self._scheduler_task:
                self._scheduler_task.cancel()
                try:
                    await self._scheduler_task
                except asyncio.CancelledError:
                    pass

            # Cancel all running sessions
            for session in self._running.values():
                if session.task and not session.task.done():
                    session.task.cancel()
                    session.status = SessionStatus.CANCELLED

            self._running.clear()
            logger.info("Session scheduler stopped")

            # Emit event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id="system",
                    payload={"cancelled_sessions": len(self._running)},
                )
                await self._event_emitter(event)

    async def pause(self) -> None:
        """Pause the scheduler (stops accepting new sessions, keeps running)."""
        async with self._lock:
            self._status = SchedulerStatus.PAUSED
            logger.info("Session scheduler paused")

            # Emit event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id="system",
                    payload={"running_sessions": len(self._running)},
                )
                await self._event_emitter(event)

    async def resume(self) -> None:
        """Resume the scheduler."""
        async with self._lock:
            self._status = SchedulerStatus.RUNNING
            logger.info("Session scheduler resumed")

            # Emit event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id="system",
                    payload={},
                )
                await self._event_emitter(event)

    def get_status(self, session_id: str) -> SessionStatus | None:
        """Get the status of a session.

        Args:
            session_id: The session ID.

        Returns:
            The session status, or None if not found.
        """
        if session_id in self._running:
            return self._running[session_id].status
        if session_id in self._completed:
            return self._completed[session_id].status
        return None

    def get_running_sessions(self) -> list[str]:
        """Get list of running session IDs."""
        return list(self._running.keys())

    def get_completed_sessions(self) -> list[str]:
        """Get list of completed session IDs."""
        return list(self._completed.keys())

    async def _run_scheduler(self) -> None:
        """Main scheduler loop - processes the queue."""
        while self._status == SchedulerStatus.RUNNING:
            try:
                # Check if we can start more sessions
                while (
                    len(self._running) < self._max_concurrent
                    and not self._queue.empty()
                ):
                    # Get next session from queue
                    _, queued = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0
                    )

                    # Check if handler exists
                    if queued.session_id not in self._session_handlers:
                        logger.warning(
                            "No handler for session %s — skipping",
                            queued.session_id
                        )
                        queued.status = SessionStatus.FAILED
                        self._completed[queued.session_id] = queued
                        continue

                    # Start execution
                    handler = self._session_handlers[queued.session_id]
                    queued.status = SessionStatus.RUNNING
                    queued.started_at = datetime.now(timezone.utc)
                    self._running[queued.session_id] = queued

                    # Create task to run the session
                    queued.task = asyncio.create_task(
                        self._execute_session(queued, handler)
                    )

                    logger.info(
                        "Started session %s (running=%d/%d)",
                        queued.session_id, len(self._running), self._max_concurrent
                    )

                # Wait before checking queue again
                await asyncio.sleep(0.5)

            except asyncio.TimeoutError:
                # No items in queue, continue
                continue
            except Exception as e:
                logger.exception("Scheduler error: %s", e)
                await asyncio.sleep(1.0)

    async def _execute_session(
        self,
        queued: QueuedSession,
        handler: Any,
    ) -> None:
        """Execute a single session.

        Args:
            queued: The queued session.
            handler: The session execution handler.
        """
        session_id = queued.session_id
        start_event_emitted = False

        try:
            logger.info("Executing session %s", session_id)

            # Emit session started event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id=session_id,
                    payload={"priority": queued.priority},
                )
                await self._event_emitter(event)
                start_event_emitted = True

            # Run the session handler
            result = await handler(queued.session_data)

            queued.status = SessionStatus.COMPLETED
            logger.info("Session %s completed successfully", session_id)

            # Emit completion event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id=session_id,
                    payload={"result": str(result)[:100]},  # Truncate result
                )
                await self._event_emitter(event)

        except asyncio.CancelledError:
            queued.status = SessionStatus.CANCELLED
            logger.info("Session %s cancelled", session_id)

            # Emit cancelled event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id=session_id,
                    payload={},
                )
                await self._event_emitter(event)

        except Exception as e:
            queued.status = SessionStatus.FAILED
            logger.exception("Session %s failed: %s", session_id, e)

            # Emit failed event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    source="scheduler",
                    session_id=session_id,
                    payload={"error": str(e)[:200]},  # Truncate error
                )
                await self._event_emitter(event)

        finally:
            # Move from running to completed
            async with self._lock:
                self._running.pop(session_id, None)
                queued.completed_at = datetime.now(timezone.utc)
                self._completed[session_id] = queued

                # Cleanup handler
                self._session_handlers.pop(session_id, None)

            logger.info(
                "Session %s finished (status=%s, running=%d, queued=%d)",
                session_id, queued.status.value, len(self._running), self._queue.qsize()
            )

    def set_max_concurrent(self, max_concurrent: int) -> None:
        """Update the maximum concurrent sessions.

        Args:
            max_concurrent: New maximum.
        """
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._max_concurrent = max_concurrent
        logger.info("Scheduler max_concurrent updated to %d", max_concurrent)


# Global scheduler instance
_scheduler: SessionScheduler | None = None


def get_scheduler() -> SessionScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        max_concurrent = int(os.environ.get("FORGE_MAX_CONCURRENT", "3"))
        _scheduler = SessionScheduler(max_concurrent=max_concurrent)
    return _scheduler


def set_scheduler(scheduler: SessionScheduler) -> None:
    """Set the global scheduler instance."""
    global _scheduler
    _scheduler = scheduler
