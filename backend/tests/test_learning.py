"""Unit tests for LearningRecorder — outcome recording for build finalization.

Tests cover:
- Record one outcome entry per executed task on build finalize (Req 25.1)
- Outcome status is exactly success or failure (Req 25.2)
- Complete build on recording failure; emit failure event (Req 25.3)
- Return recommendations derived from recorded outcomes (Req 25.4)
- Never apply recommendations automatically (Req 25.5)

Requirements: 25.1, 25.2, 25.3, 25.4, 25.5
"""

from __future__ import annotations

import pytest

from app.runtime.events.models import Event, EventType
from app.runtime.learning import (
    LearningRecorder,
    OutcomeEntry,
    OutcomeStatus,
    Recommendation,
)


# --- Helpers ---


class FakeEventEmitter:
    """Collects emitted events for test assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def last_event(self) -> Event | None:
        return self.events[-1] if self.events else None

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


class FailingEventEmitter:
    """Emitter that always raises — for testing resilience."""

    async def emit(self, event: Event) -> None:
        raise RuntimeError("Event emitter broken")


def _make_task_result(
    task_id: str = "task-1",
    outcome_status: str = "success",
    retry_count: int = 0,
    escalation_flag: bool = False,
    task_type: str = "code",
    tool: str = "aider",
    model: str = "gpt-4",
    role: str = "coder",
) -> dict:
    """Helper to create a task result dict."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "tool": tool,
        "model": model,
        "role": role,
        "outcome_status": outcome_status,
        "retry_count": retry_count,
        "escalation_flag": escalation_flag,
    }


# --- Tests for OutcomeEntry Validation ---


class TestOutcomeEntry:
    """Tests for OutcomeEntry dataclass validation."""

    def test_valid_success_entry(self) -> None:
        """A success entry with valid fields should be created."""
        entry = OutcomeEntry(
            task_id="t1",
            task_type="code",
            tool="aider",
            model="gpt-4",
            role="coder",
            outcome_status=OutcomeStatus.SUCCESS,
            retry_count=0,
            escalation_flag=False,
        )
        assert entry.outcome_status == OutcomeStatus.SUCCESS
        assert entry.retry_count == 0
        assert entry.escalation_flag is False

    def test_valid_failure_entry(self) -> None:
        """A failure entry with valid fields should be created."""
        entry = OutcomeEntry(
            task_id="t1",
            task_type="code",
            tool="aider",
            model="gpt-4",
            role="coder",
            outcome_status=OutcomeStatus.FAILURE,
            retry_count=2,
            escalation_flag=True,
        )
        assert entry.outcome_status == OutcomeStatus.FAILURE
        assert entry.retry_count == 2
        assert entry.escalation_flag is True

    def test_rejects_negative_retry_count(self) -> None:
        """Entry should reject negative retry_count (Req 25.1)."""
        with pytest.raises(ValueError, match="non-negative"):
            OutcomeEntry(
                task_id="t1",
                task_type="code",
                tool="aider",
                model="gpt-4",
                role="coder",
                outcome_status=OutcomeStatus.SUCCESS,
                retry_count=-1,
                escalation_flag=False,
            )

    def test_outcome_status_is_enum(self) -> None:
        """Outcome status should be exactly success or failure (Req 25.2)."""
        assert OutcomeStatus.SUCCESS.value == "success"
        assert OutcomeStatus.FAILURE.value == "failure"
        assert len(OutcomeStatus) == 2


# --- Tests for Recording Outcomes (Requirements 25.1, 25.2) ---


class TestRecordOutcome:
    """Tests for LearningRecorder.record_outcome()."""

    def test_records_single_outcome(self) -> None:
        """record_outcome should store the entry (Req 25.1)."""
        recorder = LearningRecorder()
        entry = OutcomeEntry(
            task_id="t1",
            task_type="code",
            tool="aider",
            model="gpt-4",
            role="coder",
            outcome_status=OutcomeStatus.SUCCESS,
            retry_count=0,
            escalation_flag=False,
            session_id="session-1",
        )

        recorder.record_outcome(entry)

        outcomes = recorder.get_outcomes("session-1")
        assert len(outcomes) == 1
        assert outcomes[0] == entry

    def test_records_with_success_status(self) -> None:
        """record_outcome should accept success status (Req 25.2)."""
        recorder = LearningRecorder()
        entry = OutcomeEntry(
            task_id="t1",
            task_type="code",
            tool="aider",
            model="gpt-4",
            role="coder",
            outcome_status=OutcomeStatus.SUCCESS,
            retry_count=0,
            escalation_flag=False,
            session_id="s1",
        )

        recorder.record_outcome(entry)

        assert recorder.get_outcomes("s1")[0].outcome_status == OutcomeStatus.SUCCESS

    def test_records_with_failure_status(self) -> None:
        """record_outcome should accept failure status (Req 25.2)."""
        recorder = LearningRecorder()
        entry = OutcomeEntry(
            task_id="t1",
            task_type="code",
            tool="aider",
            model="gpt-4",
            role="coder",
            outcome_status=OutcomeStatus.FAILURE,
            retry_count=1,
            escalation_flag=False,
            session_id="s1",
        )

        recorder.record_outcome(entry)

        assert recorder.get_outcomes("s1")[0].outcome_status == OutcomeStatus.FAILURE

    def test_records_all_required_fields(self) -> None:
        """Entry should include task_type, tool, model, role, status, retries, escalated (Req 25.1)."""
        recorder = LearningRecorder()
        entry = OutcomeEntry(
            task_id="t1",
            task_type="review",
            tool="openhands",
            model="claude-3",
            role="reviewer",
            outcome_status=OutcomeStatus.SUCCESS,
            retry_count=3,
            escalation_flag=True,
            session_id="s1",
        )

        recorder.record_outcome(entry)

        recorded = recorder.get_outcomes("s1")[0]
        assert recorded.task_type == "review"
        assert recorded.tool == "openhands"
        assert recorded.model == "claude-3"
        assert recorded.role == "reviewer"
        assert recorded.retry_count == 3
        assert recorded.escalation_flag is True

    def test_multiple_outcomes_for_session(self) -> None:
        """Multiple outcomes for same session should all be stored."""
        recorder = LearningRecorder()
        for i in range(5):
            recorder.record_outcome(
                OutcomeEntry(
                    task_id=f"t{i}",
                    task_type="code",
                    tool="aider",
                    model="gpt-4",
                    role="coder",
                    outcome_status=OutcomeStatus.SUCCESS,
                    retry_count=0,
                    escalation_flag=False,
                    session_id="s1",
                )
            )

        assert len(recorder.get_outcomes("s1")) == 5


# --- Tests for Build Outcome Recording (Requirement 25.1) ---


class TestRecordBuildOutcomes:
    """Tests for LearningRecorder.record_build_outcomes()."""

    async def test_records_one_entry_per_task(self) -> None:
        """Should record one outcome entry per executed task (Req 25.1)."""
        recorder = LearningRecorder()
        task_results = [
            _make_task_result(task_id="t1", outcome_status="success"),
            _make_task_result(task_id="t2", outcome_status="failure"),
            _make_task_result(task_id="t3", outcome_status="success"),
        ]

        await recorder.record_build_outcomes("session-1", task_results)

        outcomes = recorder.get_outcomes("session-1")
        assert len(outcomes) == 3
        assert outcomes[0].task_id == "t1"
        assert outcomes[1].task_id == "t2"
        assert outcomes[2].task_id == "t3"

    async def test_assigns_session_id(self) -> None:
        """Should assign the session_id to all entries."""
        recorder = LearningRecorder()
        task_results = [_make_task_result(task_id="t1")]

        await recorder.record_build_outcomes("session-abc", task_results)

        assert recorder.get_outcomes("session-abc")[0].session_id == "session-abc"

    async def test_maps_outcome_status(self) -> None:
        """Should correctly map outcome_status string to enum (Req 25.2)."""
        recorder = LearningRecorder()
        task_results = [
            _make_task_result(task_id="t1", outcome_status="success"),
            _make_task_result(task_id="t2", outcome_status="failure"),
        ]

        await recorder.record_build_outcomes("s1", task_results)

        outcomes = recorder.get_outcomes("s1")
        assert outcomes[0].outcome_status == OutcomeStatus.SUCCESS
        assert outcomes[1].outcome_status == OutcomeStatus.FAILURE

    async def test_empty_task_list(self) -> None:
        """Should handle an empty task list gracefully."""
        recorder = LearningRecorder()

        await recorder.record_build_outcomes("s1", [])

        assert recorder.get_outcomes("s1") == []

    async def test_maps_all_fields_from_dict(self) -> None:
        """Should map all dict fields to OutcomeEntry fields."""
        recorder = LearningRecorder()
        task_results = [
            _make_task_result(
                task_id="t1",
                task_type="review",
                tool="openhands",
                model="claude-3",
                role="reviewer",
                retry_count=2,
                escalation_flag=True,
                outcome_status="failure",
            )
        ]

        await recorder.record_build_outcomes("s1", task_results)

        entry = recorder.get_outcomes("s1")[0]
        assert entry.task_type == "review"
        assert entry.tool == "openhands"
        assert entry.model == "claude-3"
        assert entry.role == "reviewer"
        assert entry.retry_count == 2
        assert entry.escalation_flag is True
        assert entry.outcome_status == OutcomeStatus.FAILURE


# --- Tests for Recording Failure Handling (Requirement 25.3) ---


class TestRecordingFailure:
    """Tests for persistence failure handling."""

    async def test_continues_on_invalid_entry(self) -> None:
        """Should complete build on recording failure; continue with remaining tasks (Req 25.3)."""
        emitter = FakeEventEmitter()
        recorder = LearningRecorder(event_emitter=emitter)
        task_results = [
            _make_task_result(task_id="t1", outcome_status="success"),
            # Invalid: retry_count is negative -> will fail validation
            {
                "task_id": "t2",
                "task_type": "code",
                "tool": "aider",
                "model": "gpt-4",
                "role": "coder",
                "outcome_status": "success",
                "retry_count": -1,
                "escalation_flag": False,
            },
            _make_task_result(task_id="t3", outcome_status="success"),
        ]

        # Should NOT raise — build continues (Req 25.3)
        await recorder.record_build_outcomes("s1", task_results)

        # t1 and t3 should be recorded, t2 failed
        outcomes = recorder.get_outcomes("s1")
        assert len(outcomes) == 2
        assert outcomes[0].task_id == "t1"
        assert outcomes[1].task_id == "t3"

    async def test_emits_failure_event(self) -> None:
        """Should emit an error event indicating recording failure (Req 25.3)."""
        emitter = FakeEventEmitter()
        recorder = LearningRecorder(event_emitter=emitter)
        task_results = [
            {
                "task_id": "t-bad",
                "task_type": "code",
                "tool": "aider",
                "model": "gpt-4",
                "role": "coder",
                "outcome_status": "invalid_status",  # Will fail enum validation
                "retry_count": 0,
                "escalation_flag": False,
            },
        ]

        await recorder.record_build_outcomes("s1", task_results)

        assert len(emitter.events) == 1
        event = emitter.events[0]
        assert event.type == EventType.ERROR
        assert event.payload["error"] == "outcome_recording_failure"
        assert event.payload["task_id"] == "t-bad"

    async def test_failure_event_includes_task_id(self) -> None:
        """Failure event should include the affected task identifier (Req 25.3)."""
        emitter = FakeEventEmitter()
        recorder = LearningRecorder(event_emitter=emitter)
        task_results = [
            {
                "task_id": "task-xyz",
                "outcome_status": "not_a_valid_status",
            },
        ]

        await recorder.record_build_outcomes("session-abc", task_results)

        event = emitter.events[0]
        assert event.payload["task_id"] == "task-xyz"
        assert event.session_id == "session-abc"

    async def test_no_event_emitted_without_emitter(self) -> None:
        """Should not raise when no event_emitter is configured."""
        recorder = LearningRecorder()  # No emitter
        task_results = [
            {
                "task_id": "t-bad",
                "outcome_status": "invalid",
            },
        ]

        # Should not raise
        await recorder.record_build_outcomes("s1", task_results)

    async def test_resilient_to_emitter_failure(self) -> None:
        """Should not crash if event emitter itself fails (Req 25.3)."""
        emitter = FailingEventEmitter()
        recorder = LearningRecorder(event_emitter=emitter)
        task_results = [
            {
                "task_id": "t-bad",
                "outcome_status": "invalid",
            },
        ]

        # Should NOT raise even though emitter is broken
        await recorder.record_build_outcomes("s1", task_results)

    async def test_build_result_not_discarded(self) -> None:
        """Build result should not be discarded on recording failure (Req 25.3).

        After a recording failure, other tasks' outcomes should still be accessible.
        """
        emitter = FakeEventEmitter()
        recorder = LearningRecorder(event_emitter=emitter)
        task_results = [
            _make_task_result(task_id="t1", outcome_status="success"),
            {"task_id": "t2", "outcome_status": "bogus"},  # will fail
            _make_task_result(task_id="t3", outcome_status="failure"),
        ]

        await recorder.record_build_outcomes("s1", task_results)

        # Build is NOT discarded — other entries are still recorded
        outcomes = recorder.get_outcomes("s1")
        assert len(outcomes) == 2
        task_ids = [o.task_id for o in outcomes]
        assert "t1" in task_ids
        assert "t3" in task_ids


# --- Tests for Recommendations (Requirements 25.4, 25.5) ---


class TestRecommendations:
    """Tests for LearningRecorder.get_recommendations()."""

    async def test_returns_recommendations_for_session(self) -> None:
        """Should return recommendations derived from outcomes (Req 25.4)."""
        recorder = LearningRecorder()
        # Record several failures to trigger the recommendation heuristic
        for i in range(3):
            recorder.record_outcome(
                OutcomeEntry(
                    task_id=f"t{i}",
                    task_type="code",
                    tool="aider",
                    model="gpt-4",
                    role="coder",
                    outcome_status=OutcomeStatus.FAILURE,
                    retry_count=0,
                    escalation_flag=False,
                    session_id="s1",
                )
            )

        recs = recorder.get_recommendations("s1")

        assert len(recs) > 0
        assert all(isinstance(r, Recommendation) for r in recs)

    async def test_empty_recommendations_for_no_outcomes(self) -> None:
        """Should return empty list when no outcomes exist for session (Req 25.4)."""
        recorder = LearningRecorder()

        recs = recorder.get_recommendations("s1")

        assert recs == []

    async def test_recommendations_scoped_to_session(self) -> None:
        """Recommendations should consider only outcomes from the requested session."""
        recorder = LearningRecorder()
        # s1 has failures
        for i in range(3):
            recorder.record_outcome(
                OutcomeEntry(
                    task_id=f"s1-t{i}",
                    task_type="code",
                    tool="aider",
                    model="gpt-4",
                    role="coder",
                    outcome_status=OutcomeStatus.FAILURE,
                    retry_count=0,
                    escalation_flag=False,
                    session_id="s1",
                )
            )
        # s2 has only successes
        recorder.record_outcome(
            OutcomeEntry(
                task_id="s2-t1",
                task_type="code",
                tool="aider",
                model="gpt-4",
                role="coder",
                outcome_status=OutcomeStatus.SUCCESS,
                retry_count=0,
                escalation_flag=False,
                session_id="s2",
            )
        )

        recs_s1 = recorder.get_recommendations("s1")
        recs_s2 = recorder.get_recommendations("s2")

        # s1 should have tool_switch recommendation due to high failure rate
        assert len(recs_s1) > 0
        # s2 should have no recommendations (all success)
        assert recs_s2 == []

    async def test_recommendations_for_high_retry(self) -> None:
        """Should surface high-retry tasks in recommendations."""
        recorder = LearningRecorder()
        recorder.record_outcome(
            OutcomeEntry(
                task_id="t1",
                task_type="code",
                tool="aider",
                model="gpt-4",
                role="coder",
                outcome_status=OutcomeStatus.SUCCESS,
                retry_count=5,
                escalation_flag=False,
                session_id="s1",
            )
        )

        recs = recorder.get_recommendations("s1")

        assert any(r.category == "retry_tuning" for r in recs)

    def test_no_auto_apply(self) -> None:
        """LearningRecorder should never apply recommendations automatically (Req 25.5).

        The class exposes get_recommendations() (read-only) but no method that
        modifies build state or configuration based on recommendations.
        """
        recorder = LearningRecorder()

        # Verify there's no apply/execute/auto method
        public_methods = [
            m for m in dir(recorder)
            if not m.startswith("_") and callable(getattr(recorder, m))
        ]
        auto_apply_methods = [
            m for m in public_methods
            if "apply" in m.lower() or "execute" in m.lower() or "auto" in m.lower()
        ]
        assert auto_apply_methods == [], (
            f"LearningRecorder must not have auto-apply methods, found: {auto_apply_methods}"
        )

    async def test_recommendations_are_read_only(self) -> None:
        """get_recommendations should return data, not mutate state (Req 25.5)."""
        recorder = LearningRecorder()
        for i in range(3):
            recorder.record_outcome(
                OutcomeEntry(
                    task_id=f"t{i}",
                    task_type="code",
                    tool="aider",
                    model="gpt-4",
                    role="coder",
                    outcome_status=OutcomeStatus.FAILURE,
                    retry_count=0,
                    escalation_flag=False,
                    session_id="s1",
                )
            )

        recs1 = recorder.get_recommendations("s1")
        recs2 = recorder.get_recommendations("s1")

        # Calling get_recommendations multiple times produces same result
        assert len(recs1) == len(recs2)
        for r1, r2 in zip(recs1, recs2):
            assert r1.category == r2.category
            assert r1.description == r2.description


# --- Tests for get_outcomes ---


class TestGetOutcomes:
    """Tests for LearningRecorder.get_outcomes() isolation."""

    def test_returns_empty_for_unknown_session(self) -> None:
        """Should return empty list for a session with no outcomes."""
        recorder = LearningRecorder()

        assert recorder.get_outcomes("nonexistent") == []

    def test_outcomes_isolated_between_sessions(self) -> None:
        """Outcomes for one session should not appear in another."""
        recorder = LearningRecorder()
        recorder.record_outcome(
            OutcomeEntry(
                task_id="t1",
                task_type="code",
                tool="aider",
                model="gpt-4",
                role="coder",
                outcome_status=OutcomeStatus.SUCCESS,
                retry_count=0,
                escalation_flag=False,
                session_id="s1",
            )
        )
        recorder.record_outcome(
            OutcomeEntry(
                task_id="t2",
                task_type="code",
                tool="aider",
                model="gpt-4",
                role="coder",
                outcome_status=OutcomeStatus.FAILURE,
                retry_count=0,
                escalation_flag=False,
                session_id="s2",
            )
        )

        s1_outcomes = recorder.get_outcomes("s1")
        s2_outcomes = recorder.get_outcomes("s2")

        assert len(s1_outcomes) == 1
        assert s1_outcomes[0].task_id == "t1"
        assert len(s2_outcomes) == 1
        assert s2_outcomes[0].task_id == "t2"
