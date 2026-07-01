"""Build Timeout Handler - Auto-stop long-running builds.

Monitors build execution time and automatically stops builds that exceed
the configured timeout. This prevents builds from running indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Default timeout in seconds (30 minutes)
DEFAULT_BUILD_TIMEOUT = int(os.environ.get("FORGE_BUILD_TIMEOUT_SECONDS", "1800"))


@dataclass
class BuildTimeout:
    """Represents the timeout state for a build session."""
    session_id: str
    started_at: datetime
    timeout_seconds: int
    task: asyncio.Task | None = None
    cancelled: bool = False


class BuildTimeoutManager:
    """Manages build timeouts for all active sessions.

    Automatically stops builds that exceed the configured timeout.
    """

    def __init__(
        self,
        default_timeout_seconds: int = DEFAULT_BUILD_TIMEOUT,
        event_emitter: Any | None = None,
        interrupt_handler: Any | None = None,
    ) -> None:
        """Initialize the BuildTimeoutManager.

        Args:
            default_timeout_seconds: Default timeout for builds (in seconds).
            event_emitter: Optional event bus publisher.
            interrupt_handler: Optional interrupt handler for stopping builds.
        """
        self._default_timeout = default_timeout_seconds
        self._event_emitter = event_emitter
        self._interrupt_handler = interrupt_handler
        self._builds: dict[str, BuildTimeout] = {}
        self._lock = asyncio.Lock()

    def update_event_emitter(self, event_emitter: Any) -> None:
        """Update the event emitter after initialization.

        Args:
            event_emitter: Event bus publisher function.
        """
        self._event_emitter = event_emitter
        logger.debug("BuildTimeoutManager event emitter updated")

    def update_interrupt_handler(self, interrupt_handler: Any) -> None:
        """Update the interrupt handler after initialization.

        Args:
            interrupt_handler: Interrupt handler function.
        """
        self._interrupt_handler = interrupt_handler
        logger.debug("BuildTimeoutManager interrupt handler updated")

    async def start_tracking(
        self,
        session_id: str,
        timeout_seconds: int | None = None,
    ) -> None:
        """Start tracking a build session's execution time.

        Args:
            session_id: The session ID to track.
            timeout_seconds: Optional custom timeout. Defaults to configured value.
        """
        async with self._lock:
            # Cancel any existing tracking for this session
            await self.stop_tracking(session_id)

            timeout = timeout_seconds or self._default_timeout
            build = BuildTimeout(
                session_id=session_id,
                started_at=datetime.now(timezone.utc),
                timeout_seconds=timeout,
            )

            # Create a task to track the timeout
            build.task = asyncio.create_task(
                self._track_build(build)
            )

            self._builds[session_id] = build

            logger.info(
                "Started tracking build timeout for session %s (timeout=%ds)",
                session_id, timeout
            )

            # Emit event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    type=EventType.BUILD_TIMEOUT_STARTED,
                    session_id=session_id,
                    source="build_timeout_manager",
                    payload={
                        "timeout_seconds": timeout,
                        "started_at": build.started_at.isoformat(),
                    },
                    correlation_id=session_id,
                    event_id=str(uuid.uuid4()),
                )
                await self._event_emitter(event)

    async def stop_tracking(self, session_id: str) -> None:
        """Stop tracking a build session.

        Args:
            session_id: The session ID to stop tracking.
        """
        async with self._lock:
            if session_id not in self._builds:
                return

            build = self._builds[session_id]
            build.cancelled = True

            if build.task and not build.task.done():
                build.task.cancel()
                try:
                    await build.task
                except asyncio.CancelledError:
                    pass

            del self._builds[session_id]

            logger.info("Stopped tracking build timeout for session %s", session_id)

    async def extend_timeout(
        self,
        session_id: str,
        additional_seconds: int,
    ) -> bool:
        """Extend the timeout for a build session.

        Args:
            session_id: The session ID.
            additional_seconds: Additional seconds to add.

        Returns:
            True if extended, False if session not found.
        """
        async with self._lock:
            if session_id not in self._builds:
                return False

            build = self._builds[session_id]
            build.timeout_seconds += additional_seconds

            logger.info(
                "Extended timeout for session %s by %ds (new timeout=%ds)",
                session_id, additional_seconds, build.timeout_seconds
            )

            return True

    async def get_remaining_time(self, session_id: str) -> int | None:
        """Get remaining time in seconds for a build session.

        Args:
            session_id: The session ID.

        Returns:
            Remaining seconds, or None if not tracked.
        """
        async with self._lock:
            if session_id not in self._builds:
                return None

            build = self._builds[session_id]
            elapsed = datetime.now(timezone.utc) - build.started_at
            remaining = build.timeout_seconds - int(elapsed.total_seconds())

            return max(0, remaining)

    async def _track_build(self, build: BuildTimeout) -> None:
        """Track a build and auto-stop on timeout.

        Args:
            build: The BuildTimeout to track.
        """
        try:
            # Wait for timeout
            await asyncio.sleep(build.timeout_seconds)

            # Check if cancelled
            if build.cancelled:
                return

            logger.warning(
                "Build timeout exceeded for session %s — stopping build",
                build.session_id
            )

            # Stop the build
            if self._interrupt_handler:
                try:
                    await self._interrupt_handler.stop(build.session_id)
                    logger.info(
                        "Build %s stopped due to timeout",
                        build.session_id
                    )
                except Exception as e:
                    logger.error(
                        "Failed to stop build %s: %s",
                        build.session_id, e
                    )

            # Emit timeout event
            if self._event_emitter:
                from app.runtime.events.models import Event, EventType
                event = Event.create(
                    type=EventType.BUILD_TIMEOUT_EXCEEDED,
                    session_id=build.session_id,
                    source="build_timeout_manager",
                    payload={
                        "timeout_seconds": build.timeout_seconds,
                        "elapsed_seconds": build.timeout_seconds,
                    },
                    correlation_id=build.session_id,
                    event_id=str(uuid.uuid4()),
                )
                await self._event_emitter(event)

        except asyncio.CancelledError:
            # Build was stopped externally
            logger.info(
                "Timeout tracking cancelled for session %s",
                build.session_id
            )
        except Exception as e:
            logger.exception(
                "Error tracking timeout for session %s: %s",
                build.session_id, e
            )

    def is_tracked(self, session_id: str) -> bool:
        """Check if a session is being tracked.

        Args:
            session_id: The session ID.

        Returns:
            True if tracked, False otherwise.
        """
        return session_id in self._builds


# Global timeout manager instance
_timeout_manager: BuildTimeoutManager | None = None


def get_timeout_manager() -> BuildTimeoutManager:
    """Get the global timeout manager instance."""
    global _timeout_manager
    if _timeout_manager is None:
        timeout_seconds = int(os.environ.get("FORGE_BUILD_TIMEOUT_SECONDS", str(DEFAULT_BUILD_TIMEOUT)))
        _timeout_manager = BuildTimeoutManager(default_timeout_seconds=timeout_seconds)
    return _timeout_manager


def set_timeout_manager(manager: BuildTimeoutManager) -> None:
    """Set the global timeout manager instance."""
    global _timeout_manager
    _timeout_manager = manager
