"""Unit tests for InterruptHandler — pause, resume, redirect, and stop.

Tests cover:
- Pause execution within 2 seconds; retain all build state
- Surface interrupt message to developer within 2 seconds of pausing
- Resume from paused point using retained state; emit interrupt.resumed within 2s
- Reject resume when no interrupt is active; return error
- Redirect: return to planning seeded with new direction (non-empty)
- Reject redirect with empty direction; keep paused state
- Cancel in-flight model backoff within 2 seconds on stop/interrupt

Requirements: 24.1, 24.2, 24.3, 24.4, 24.5, 24.6, 24.7
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.runtime.events.models import Event, EventType
from app.runtime.interrupt import (
    EmptyDirectionError,
    InterruptHandler,
    NotPausedError,
    PausedState,
)


# --- Helpers ---


class EventCollector:
    """Collects emitted events for test assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def last_event(self) -> Event | None:
        return self.events[-1] if self.events else None

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


class BackoffTracker:
    """Tracks cancel_backoff calls."""

    def __init__(self) -> None:
        self.cancel_count = 0

    def cancel_backoff(self) -> None:
        self.cancel_count += 1


class PlanningTracker:
    """Tracks planning callback invocations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def plan(self, session_id: str, direction: str) -> None:
        self.calls.append((session_id, direction))


# --- Tests for Pause (Requirements 24.1, 24.2, 24.7) ---


class TestPause:
    """Tests for InterruptHandler.pause()."""

    async def test_pause_sets_paused_state(self) -> None:
        """Pause should set is_paused=True and retain the build state."""
        handler = InterruptHandler()
        build_state = {"current_task": "task-1", "progress": 50}

        result = await handler.pause("session-1", "Developer interrupt", build_state)

        assert result.is_paused is True
        assert result.retained_state == build_state
        assert result.interrupt_message == "Developer interrupt"
        assert result.paused_at > 0

    async def test_pause_retains_build_state(self) -> None:
        """Pause should retain all in-progress build state (Req 24.1)."""
        handler = InterruptHandler()
        complex_state = {
            "tasks": ["t1", "t2", "t3"],
            "current_task_id": "t2",
            "decisions": [{"kind": "retry", "count": 1}],
            "workspace_id": "ws-abc",
        }

        await handler.pause("session-1", "Hold on", complex_state)

        assert handler.get_retained_state("session-1") == complex_state

    async def test_pause_surfaces_message(self) -> None:
        """Pause should surface the interrupt message (Req 24.2)."""
        handler = InterruptHandler()

        await handler.pause("session-1", "Please wait")

        assert handler.get_interrupt_message("session-1") == "Please wait"

    async def test_pause_emits_interrupt_paused_event(self) -> None:
        """Pause should emit an interrupt.paused event (Req 24.2)."""
        collector = EventCollector()
        handler = InterruptHandler(event_emitter=collector.emit)

        await handler.pause("session-1", "Pausing now", {"step": 3})

        assert len(collector.events) == 1
        event = collector.events[0]
        assert event.type == EventType.INTERRUPT_PAUSED
        assert event.session_id == "session-1"
        assert event.payload["message"] == "Pausing now"
        assert event.payload["session_id"] == "session-1"

    async def test_pause_cancels_backoff(self) -> None:
        """Pause should cancel in-flight model backoff (Req 24.7)."""
        tracker = BackoffTracker()
        handler = InterruptHandler(cancel_backoff=tracker.cancel_backoff)

        await handler.pause("session-1", "Interrupt")

        assert tracker.cancel_count == 1

    async def test_pause_completes_within_2_seconds(self) -> None:
        """Pause operation should complete within 2 seconds (Req 24.1)."""
        handler = InterruptHandler()
        start = time.monotonic()

        await handler.pause("session-1", "Fast pause", {"data": "x"})

        elapsed = time.monotonic() - start
        assert elapsed < 2.0

    async def test_pause_with_no_build_state(self) -> None:
        """Pause with no build_state should retain empty dict."""
        handler = InterruptHandler()

        result = await handler.pause("session-1", "Pause")

        assert result.retained_state == {}

    async def test_is_paused_returns_true_after_pause(self) -> None:
        """is_paused should return True after pausing."""
        handler = InterruptHandler()

        await handler.pause("session-1", "test")

        assert handler.is_paused("session-1") is True

    async def test_is_paused_returns_false_initially(self) -> None:
        """is_paused should return False for sessions that were never paused."""
        handler = InterruptHandler()

        assert handler.is_paused("session-1") is False


# --- Tests for Resume (Requirements 24.3, 24.4) ---


class TestResume:
    """Tests for InterruptHandler.resume()."""

    async def test_resume_returns_retained_state(self) -> None:
        """Resume should return the retained build state (Req 24.3)."""
        handler = InterruptHandler()
        build_state = {"task": "t1", "step": 5}
        await handler.pause("session-1", "Pause", build_state)

        result = await handler.resume("session-1")

        assert result == build_state

    async def test_resume_clears_paused_state(self) -> None:
        """Resume should clear the paused state (Req 24.3)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause", {"data": 1})

        await handler.resume("session-1")

        assert handler.is_paused("session-1") is False
        assert handler.get_interrupt_message("session-1") == ""
        assert handler.get_retained_state("session-1") == {}

    async def test_resume_emits_interrupt_resumed_event(self) -> None:
        """Resume should emit an interrupt.resumed event (Req 24.3)."""
        collector = EventCollector()
        handler = InterruptHandler(event_emitter=collector.emit)
        await handler.pause("session-1", "Pause")

        await handler.resume("session-1")

        resumed_events = collector.events_of_type(EventType.INTERRUPT_RESUMED)
        assert len(resumed_events) == 1
        event = resumed_events[0]
        assert event.session_id == "session-1"
        assert "resumed_at" in event.payload

    async def test_resume_rejects_when_not_paused(self) -> None:
        """Resume should raise NotPausedError when no interrupt is active (Req 24.4)."""
        handler = InterruptHandler()

        with pytest.raises(NotPausedError) as exc_info:
            await handler.resume("session-1")

        assert "session-1" in str(exc_info.value)

    async def test_resume_rejects_after_already_resumed(self) -> None:
        """Resume should reject a second resume after already resuming (Req 24.4)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause", {"x": 1})
        await handler.resume("session-1")

        with pytest.raises(NotPausedError):
            await handler.resume("session-1")

    async def test_resume_completes_within_2_seconds(self) -> None:
        """Resume operation should complete within 2 seconds (Req 24.3)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause", {"state": "data"})

        start = time.monotonic()
        await handler.resume("session-1")
        elapsed = time.monotonic() - start

        assert elapsed < 2.0


# --- Tests for Redirect (Requirements 24.5, 24.6) ---


class TestRedirect:
    """Tests for InterruptHandler.redirect()."""

    async def test_redirect_with_valid_direction(self) -> None:
        """Redirect with non-empty direction should succeed (Req 24.5)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause")

        result = await handler.redirect("session-1", "Use React instead of Vue")

        assert result["direction"] == "Use React instead of Vue"
        assert result["session_id"] == "session-1"

    async def test_redirect_clears_paused_state(self) -> None:
        """Redirect should clear paused state (Req 24.5)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause", {"old": "state"})

        await handler.redirect("session-1", "New direction")

        assert handler.is_paused("session-1") is False

    async def test_redirect_invokes_planning_callback(self) -> None:
        """Redirect should invoke the planning callback with direction (Req 24.5)."""
        planner = PlanningTracker()
        handler = InterruptHandler(planning_callback=planner.plan)
        await handler.pause("session-1", "Pause")

        await handler.redirect("session-1", "Switch to FastAPI")

        assert len(planner.calls) == 1
        assert planner.calls[0] == ("session-1", "Switch to FastAPI")

    async def test_redirect_emits_interrupt_redirected_event(self) -> None:
        """Redirect should emit an interrupt.redirected event."""
        collector = EventCollector()
        handler = InterruptHandler(event_emitter=collector.emit)
        await handler.pause("session-1", "Pause")

        await handler.redirect("session-1", "New goal")

        redirected_events = collector.events_of_type(EventType.INTERRUPT_REDIRECTED)
        assert len(redirected_events) == 1
        assert redirected_events[0].payload["direction"] == "New goal"

    async def test_redirect_rejects_empty_direction(self) -> None:
        """Redirect should reject empty direction (Req 24.6)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause")

        with pytest.raises(EmptyDirectionError) as exc_info:
            await handler.redirect("session-1", "")

        assert "session-1" in str(exc_info.value)

    async def test_redirect_rejects_whitespace_only_direction(self) -> None:
        """Redirect should reject whitespace-only direction (Req 24.6)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause")

        with pytest.raises(EmptyDirectionError):
            await handler.redirect("session-1", "   \t\n  ")

    async def test_redirect_keeps_paused_state_on_empty_direction(self) -> None:
        """Redirect rejection should keep the build in paused state (Req 24.6)."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause", {"retained": True})

        with pytest.raises(EmptyDirectionError):
            await handler.redirect("session-1", "")

        # State should remain paused
        assert handler.is_paused("session-1") is True
        assert handler.get_retained_state("session-1") == {"retained": True}

    async def test_redirect_rejects_when_not_paused(self) -> None:
        """Redirect should reject if the session is not paused."""
        handler = InterruptHandler()

        with pytest.raises(NotPausedError):
            await handler.redirect("session-1", "New direction")

    async def test_redirect_strips_whitespace_from_direction(self) -> None:
        """Redirect should strip leading/trailing whitespace from direction."""
        planner = PlanningTracker()
        handler = InterruptHandler(planning_callback=planner.plan)
        await handler.pause("session-1", "Pause")

        result = await handler.redirect("session-1", "  Focus on tests  ")

        assert result["direction"] == "Focus on tests"
        assert planner.calls[0][1] == "Focus on tests"


# --- Tests for Stop (Requirement 24.7) ---


class TestStop:
    """Tests for InterruptHandler.stop()."""

    async def test_stop_cancels_backoff(self) -> None:
        """Stop should cancel in-flight model backoff (Req 24.7)."""
        tracker = BackoffTracker()
        handler = InterruptHandler(cancel_backoff=tracker.cancel_backoff)

        await handler.stop("session-1")

        assert tracker.cancel_count == 1

    async def test_stop_clears_paused_state(self) -> None:
        """Stop should clear any paused state."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause", {"state": "x"})

        await handler.stop("session-1")

        assert handler.is_paused("session-1") is False
        assert handler.get_retained_state("session-1") == {}

    async def test_stop_emits_interrupt_stopped_event(self) -> None:
        """Stop should emit an interrupt.stopped event."""
        collector = EventCollector()
        handler = InterruptHandler(event_emitter=collector.emit)

        await handler.stop("session-1")

        stopped_events = collector.events_of_type(EventType.INTERRUPT_STOPPED)
        assert len(stopped_events) == 1
        assert stopped_events[0].session_id == "session-1"

    async def test_stop_completes_within_2_seconds(self) -> None:
        """Stop operation should complete within 2 seconds (Req 24.7)."""
        handler = InterruptHandler()

        start = time.monotonic()
        await handler.stop("session-1")
        elapsed = time.monotonic() - start

        assert elapsed < 2.0

    async def test_stop_without_cancel_callback(self) -> None:
        """Stop should work even without a cancel_backoff callback."""
        handler = InterruptHandler()

        # Should not raise
        await handler.stop("session-1")


# --- Tests for Multi-session Isolation ---


class TestMultiSession:
    """Tests for per-session state isolation."""

    async def test_pause_one_session_does_not_affect_another(self) -> None:
        """Pausing one session should not affect another."""
        handler = InterruptHandler()

        await handler.pause("session-1", "Pause 1", {"s": 1})

        assert handler.is_paused("session-1") is True
        assert handler.is_paused("session-2") is False

    async def test_independent_resume(self) -> None:
        """Each session should resume independently."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause 1", {"a": 1})
        await handler.pause("session-2", "Pause 2", {"b": 2})

        result1 = await handler.resume("session-1")

        assert result1 == {"a": 1}
        assert handler.is_paused("session-1") is False
        assert handler.is_paused("session-2") is True

    async def test_stop_one_session_does_not_affect_another(self) -> None:
        """Stopping one session should not affect another."""
        handler = InterruptHandler()
        await handler.pause("session-1", "Pause 1", {"x": 1})
        await handler.pause("session-2", "Pause 2", {"y": 2})

        await handler.stop("session-1")

        assert handler.is_paused("session-1") is False
        assert handler.is_paused("session-2") is True


# --- Integration with CancellableBackoff (Requirement 24.7) ---


class TestBackoffCancellation:
    """Tests for integration with ModelRouter's cancel_backoff."""

    async def test_pause_cancels_inflight_backoff_via_cancel_event(self) -> None:
        """Pause should cancel in-flight backoff via the cancel event (Req 24.7)."""
        from app.runtime.router import CancellableBackoff

        cancel_event = asyncio.Event()
        backoff = CancellableBackoff(cancel_event=cancel_event)

        handler = InterruptHandler(cancel_backoff=cancel_event.set)

        # Start a long backoff in background
        backoff_task = asyncio.create_task(backoff.wait(5))  # ~30s backoff

        # Give it a moment to start sleeping
        await asyncio.sleep(0.01)
        assert backoff.is_sleeping

        # Pause should cancel the backoff
        await handler.pause("session-1", "Interrupt")

        # Backoff should resolve quickly (cancelled)
        result = await asyncio.wait_for(backoff_task, timeout=2.0)
        assert result is False  # False means cancelled

    async def test_stop_cancels_inflight_backoff_via_cancel_event(self) -> None:
        """Stop should cancel in-flight backoff via the cancel event (Req 24.7)."""
        from app.runtime.router import CancellableBackoff

        cancel_event = asyncio.Event()
        backoff = CancellableBackoff(cancel_event=cancel_event)

        handler = InterruptHandler(cancel_backoff=cancel_event.set)

        # Start a long backoff in background
        backoff_task = asyncio.create_task(backoff.wait(5))

        # Give it a moment to start sleeping
        await asyncio.sleep(0.01)
        assert backoff.is_sleeping

        # Stop should cancel the backoff
        await handler.stop("session-1")

        # Backoff should resolve quickly (cancelled)
        result = await asyncio.wait_for(backoff_task, timeout=2.0)
        assert result is False  # False means cancelled
