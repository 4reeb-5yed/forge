"""Interrupt Handler — manages build state during interrupts.

Provides pause, resume, redirect, and stop operations for active build sessions.
All operations are state transitions that complete within 2 seconds.

Integrates with the ModelRouter's cancel_backoff() for immediate cancellation
of in-flight retries on stop/interrupt.

Requirements: 24.1, 24.2, 24.3, 24.4, 24.5, 24.6, 24.7
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InterruptError(Exception):
    """Base error for interrupt operations."""

    pass


class NotPausedError(InterruptError):
    """Raised when resume is requested but no interrupt is active.

    Per requirement 24.4: reject resume when no interrupt is active.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"Cannot resume session '{session_id}': no paused build exists"
        )


class EmptyDirectionError(InterruptError):
    """Raised when redirect is requested with an empty direction.

    Per requirement 24.6: reject redirect with empty direction.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"Cannot redirect session '{session_id}': a non-empty direction is required"
        )


# ---------------------------------------------------------------------------
# Paused state
# ---------------------------------------------------------------------------


@dataclass
class PausedState:
    """Retained build state during an interrupt.

    Attributes:
        is_paused: Whether the session is currently paused.
        paused_at: Monotonic timestamp when the pause occurred.
        retained_state: The build state snapshot retained for resumption.
        interrupt_message: The message surfaced to the developer.
    """

    is_paused: bool = False
    paused_at: float = 0.0
    retained_state: dict[str, Any] = field(default_factory=dict)
    interrupt_message: str = ""


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# Callback to cancel in-flight model backoff (from ModelRouter)
CancelBackoffCallback = Callable[[], None]

# Async event emitter callback
EventEmitter = Callable[[Event], Awaitable[Any]]

# Callback to re-enter planning with a new direction
PlanningCallback = Callable[[str, str], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Interrupt Handler
# ---------------------------------------------------------------------------


class InterruptHandler:
    """Manages build state during interrupts: pause, resume, redirect, stop.

    Tracks paused state per session and integrates with the ModelRouter's
    cancel_backoff() for the 2-second response requirement on stop/interrupt.

    All operations are synchronous state transitions (no AI calls) and
    complete within 2 seconds.

    Requirements:
        24.1 — Pause execution within 2s, retain all build state
        24.2 — Surface interrupt message within 2s of pausing
        24.3 — Resume from paused point, emit interrupt.resumed within 2s
        24.4 — Reject resume when no interrupt is active
        24.5 — Redirect: return to planning seeded with new direction
        24.6 — Reject redirect with empty direction, keep paused state
        24.7 — Cancel in-flight model backoff within 2s on stop/interrupt
    """

    def __init__(
        self,
        *,
        cancel_backoff: CancelBackoffCallback | None = None,
        event_emitter: EventEmitter | None = None,
        planning_callback: PlanningCallback | None = None,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the InterruptHandler.

        Args:
            cancel_backoff: Callable that cancels any in-flight model backoff.
                Typically ModelRouter.cancel_backoff.
            event_emitter: Async callable that emits Events for observability.
            planning_callback: Async callable to re-enter planning with a new
                direction. Signature: (session_id, direction) -> Any.
            time_func: Optional time function for testing (defaults to time.monotonic).
        """
        self._cancel_backoff = cancel_backoff
        self._event_emitter = event_emitter
        self._planning_callback = planning_callback
        self._time_func = time_func or time.monotonic
        self._sessions: dict[str, PausedState] = {}

    def _get_state(self, session_id: str) -> PausedState:
        """Get or create the paused state for a session."""
        if session_id not in self._sessions:
            self._sessions[session_id] = PausedState()
        return self._sessions[session_id]

    def is_paused(self, session_id: str) -> bool:
        """Check whether a session is currently paused."""
        state = self._sessions.get(session_id)
        return state.is_paused if state else False

    def get_interrupt_message(self, session_id: str) -> str:
        """Get the interrupt message for a paused session."""
        state = self._sessions.get(session_id)
        return state.interrupt_message if state else ""

    def get_retained_state(self, session_id: str) -> dict[str, Any]:
        """Get the retained build state for a paused session."""
        state = self._sessions.get(session_id)
        return state.retained_state if state else {}

    async def pause(
        self,
        session_id: str,
        message: str,
        build_state: dict[str, Any] | None = None,
    ) -> PausedState:
        """Pause execution, retain build state, and emit interrupt.paused event.

        Per requirements 24.1 and 24.2:
        - Pauses execution within 2 seconds
        - Retains all in-progress build state for later resumption
        - Surfaces the interrupt message to the developer within 2 seconds

        Also cancels any in-flight model backoff per requirement 24.7.

        Args:
            session_id: The session to pause.
            message: The interrupt message to surface to the developer.
            build_state: The build state to retain for resumption.

        Returns:
            The PausedState with retained state and message.
        """
        # Cancel in-flight backoff immediately (Requirement 24.7)
        if self._cancel_backoff is not None:
            self._cancel_backoff()

        state = self._get_state(session_id)
        state.is_paused = True
        state.paused_at = self._time_func()
        state.retained_state = build_state or {}
        state.interrupt_message = message

        logger.info(
            "Session '%s' paused with message: %s",
            session_id,
            message,
        )

        # Emit interrupt.paused event (Requirement 24.2)
        await self._emit_event(
            session_id=session_id,
            event_type=EventType.INTERRUPT_PAUSED,
            payload={
                "session_id": session_id,
                "message": message,
                "paused_at": state.paused_at,
            },
        )

        return state

    async def resume(self, session_id: str) -> dict[str, Any]:
        """Resume from paused point using retained state.

        Per requirement 24.3: continues execution from the paused point
        using the retained build state and emits interrupt.resumed within 2s.

        Per requirement 24.4: rejects resume when no interrupt is active.

        Args:
            session_id: The session to resume.

        Returns:
            The retained build state to continue execution with.

        Raises:
            NotPausedError: If the session is not currently paused.
        """
        state = self._sessions.get(session_id)

        if state is None or not state.is_paused:
            raise NotPausedError(session_id)

        # Capture state before clearing
        retained = state.retained_state.copy()

        # Clear paused state
        state.is_paused = False
        state.paused_at = 0.0
        state.retained_state = {}
        state.interrupt_message = ""

        logger.info("Session '%s' resumed", session_id)

        # Emit interrupt.resumed event (Requirement 24.3)
        await self._emit_event(
            session_id=session_id,
            event_type=EventType.INTERRUPT_RESUMED,
            payload={
                "session_id": session_id,
                "resumed_at": self._time_func(),
            },
        )

        return retained

    async def redirect(self, session_id: str, direction: str) -> dict[str, Any]:
        """Change build direction during an interrupt.

        Per requirement 24.5: returns to planning seeded with the new direction.
        Per requirement 24.6: rejects redirect with empty direction, keeps
        the build in paused state.

        Args:
            session_id: The session to redirect.
            direction: The new direction for the build (must be non-empty).

        Returns:
            Dict with the new direction and session info.

        Raises:
            EmptyDirectionError: If direction is empty or whitespace-only.
            NotPausedError: If the session is not currently paused.
        """
        # Validate direction is non-empty (Requirement 24.6)
        if not direction or not direction.strip():
            raise EmptyDirectionError(session_id)

        state = self._sessions.get(session_id)
        if state is None or not state.is_paused:
            raise NotPausedError(session_id)

        # Clear paused state since we're redirecting
        state.is_paused = False
        state.paused_at = 0.0
        state.retained_state = {}
        state.interrupt_message = ""

        logger.info(
            "Session '%s' redirected with direction: %s",
            session_id,
            direction.strip(),
        )

        # Emit interrupt.redirected event
        await self._emit_event(
            session_id=session_id,
            event_type=EventType.INTERRUPT_REDIRECTED,
            payload={
                "session_id": session_id,
                "direction": direction.strip(),
            },
        )

        # Invoke planning callback if configured (Requirement 24.5)
        if self._planning_callback is not None:
            await self._planning_callback(session_id, direction.strip())

        return {"session_id": session_id, "direction": direction.strip()}

    async def stop(self, session_id: str) -> None:
        """Stop the build entirely, cancelling any in-flight backoff.

        Per requirement 24.7: cancels in-flight model backoff within 2 seconds
        and does not issue further retry attempts.

        Args:
            session_id: The session to stop.
        """
        # Cancel in-flight backoff immediately (Requirement 24.7)
        if self._cancel_backoff is not None:
            self._cancel_backoff()

        # Clear any paused state
        state = self._get_state(session_id)
        state.is_paused = False
        state.paused_at = 0.0
        state.retained_state = {}
        state.interrupt_message = ""

        logger.info("Session '%s' stopped", session_id)

        # Emit interrupt.stopped event
        await self._emit_event(
            session_id=session_id,
            event_type=EventType.INTERRUPT_STOPPED,
            payload={"session_id": session_id},
        )

    async def _emit_event(
        self,
        *,
        session_id: str,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> None:
        """Emit an event via the configured event emitter."""
        if self._event_emitter is None:
            return

        event = Event.create(
            type=event_type,
            session_id=session_id,
            source="interrupt_handler",
            payload=payload,
        )
        await self._event_emitter(event)
