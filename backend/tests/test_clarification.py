"""Unit tests for the clarification workflow with SessionContext.

Validates requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6.
"""

from __future__ import annotations

import pytest

from app.runtime.clarification import (
    DEFAULT_MAX_QUESTIONS,
    REQUIRED_INPUTS,
    SessionContext,
    SpecificationInput,
    advance_if_ready,
    emit_question,
    get_missing_inputs,
    handle_answer,
    record_answer,
    run_clarification,
)
from app.runtime.events.models import Event, EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeEventEmitter:
    """A fake event emitter that records published events."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._seq = 0

    async def __call__(self, event: Event) -> Event:
        self._seq += 1
        published = Event(
            schema_version=event.schema_version,
            seq=self._seq,
            session_id=event.session_id,
            type=event.type,
            timestamp=event.timestamp,
            source=event.source,
            payload=event.payload,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
            event_id=event.event_id,
        )
        self.events.append(published)
        return published


def _empty_context(session_id: str = "sess-1") -> SessionContext:
    """Create a SessionContext with no inputs filled."""
    return SessionContext(session_id=session_id)


def _full_context(session_id: str = "sess-1") -> SessionContext:
    """Create a SessionContext with all required inputs filled."""
    return SessionContext(
        session_id=session_id,
        goals=["Build a REST API for user management"],
        constraints=["Must use PostgreSQL, no ORM"],
        preferences=["FastAPI, Python 3.11"],
    )


def _partial_context(session_id: str = "sess-1") -> SessionContext:
    """Create a SessionContext with some inputs filled."""
    return SessionContext(
        session_id=session_id,
        goals=["Build a REST API"],
        # constraints and preferences are empty
    )


# ---------------------------------------------------------------------------
# Requirement 3.3: Deterministic serialization with fixed field order
# ---------------------------------------------------------------------------


class TestSessionContextSerialization:
    """Test deterministic serialization of SessionContext."""

    def test_empty_context_serializes_to_empty_string(self) -> None:
        ctx = _empty_context()
        assert ctx.serialize_to_prompt() == ""

    def test_full_context_produces_deterministic_output(self) -> None:
        ctx = _full_context()
        result = ctx.serialize_to_prompt()

        # Same context should always produce same output
        for _ in range(100):
            assert ctx.serialize_to_prompt() == result

    def test_field_order_is_goals_decisions_assumptions_constraints_preferences(self) -> None:
        """Verify the fixed declared field order."""
        ctx = SessionContext(
            session_id="s1",
            goals=["g1"],
            decisions=["d1"],
            assumptions=["a1"],
            constraints=["c1"],
            preferences=["p1"],
        )
        result = ctx.serialize_to_prompt()

        # Check that Goals appears before Decisions, etc.
        goals_pos = result.index("Goals:")
        decisions_pos = result.index("Decisions:")
        assumptions_pos = result.index("Assumptions:")
        constraints_pos = result.index("Constraints:")
        preferences_pos = result.index("Preferences:")

        assert goals_pos < decisions_pos < assumptions_pos < constraints_pos < preferences_pos

    def test_identical_content_produces_identical_output(self) -> None:
        """Two contexts with same content must serialize identically."""
        ctx1 = SessionContext(
            session_id="s1",
            goals=["build API"],
            constraints=["use Python"],
        )
        ctx2 = SessionContext(
            session_id="s2",  # different session_id
            goals=["build API"],
            constraints=["use Python"],
        )
        # session_id is not part of the prompt serialization
        assert ctx1.serialize_to_prompt() == ctx2.serialize_to_prompt()

    def test_serialization_includes_all_items_in_field(self) -> None:
        ctx = SessionContext(
            session_id="s1",
            goals=["g1", "g2", "g3"],
            constraints=["c1", "c2"],
        )
        result = ctx.serialize_to_prompt()
        assert "- g1" in result
        assert "- g2" in result
        assert "- g3" in result
        assert "- c1" in result
        assert "- c2" in result

    def test_empty_fields_are_skipped(self) -> None:
        ctx = SessionContext(
            session_id="s1",
            goals=["g1"],
            # decisions, assumptions, constraints, preferences are empty
        )
        result = ctx.serialize_to_prompt()
        assert "Goals:" in result
        assert "Decisions:" not in result
        assert "Assumptions:" not in result
        assert "Constraints:" not in result
        assert "Preferences:" not in result


# ---------------------------------------------------------------------------
# Requirement 3.1: Emit clarification questions for missing inputs
# ---------------------------------------------------------------------------


class TestGetMissingInputs:
    """Test identification of missing specification inputs."""

    def test_all_missing_when_context_is_empty(self) -> None:
        ctx = _empty_context()
        missing = get_missing_inputs(ctx)
        assert len(missing) == len(REQUIRED_INPUTS)

    def test_none_missing_when_context_is_full(self) -> None:
        ctx = _full_context()
        missing = get_missing_inputs(ctx)
        assert missing == []

    def test_partial_context_identifies_missing_fields(self) -> None:
        ctx = _partial_context()
        missing = get_missing_inputs(ctx)
        # Goals are filled, but constraints and preferences are not
        missing_names = [m.name for m in missing]
        assert "goal_refinement" not in missing_names
        assert "constraints" in missing_names
        assert "tech_stack_preferences" in missing_names

    def test_custom_required_inputs(self) -> None:
        custom_inputs = [
            SpecificationInput(
                name="custom_input",
                question="Custom question?",
                context_field="goals",
            ),
        ]
        ctx = _empty_context()
        missing = get_missing_inputs(ctx, required_inputs=custom_inputs)
        assert len(missing) == 1
        assert missing[0].name == "custom_input"


class TestEmitQuestion:
    """Test clarification question event emission."""

    @pytest.mark.asyncio
    async def test_emits_question_event(self) -> None:
        emitter = FakeEventEmitter()
        spec_input = REQUIRED_INPUTS[0]

        event = await emit_question(
            spec_input,
            emitter,
            session_id="sess-1",
        )

        assert event.type == EventType.QUESTION
        assert event.payload["input_name"] == spec_input.name
        assert event.payload["question"] == spec_input.question
        assert event.payload["requires_answer"] is True
        assert event.source == "clarification"

    @pytest.mark.asyncio
    async def test_emitted_event_has_session_id(self) -> None:
        emitter = FakeEventEmitter()
        event = await emit_question(
            REQUIRED_INPUTS[0],
            emitter,
            session_id="my-session",
        )
        assert event.session_id == "my-session"
        assert event.correlation_id == "my-session"

    @pytest.mark.asyncio
    async def test_emitted_event_has_causation_id(self) -> None:
        emitter = FakeEventEmitter()
        event = await emit_question(
            REQUIRED_INPUTS[0],
            emitter,
            session_id="s1",
            causation_id="cause-123",
        )
        assert event.causation_id == "cause-123"


class TestRunClarification:
    """Test the full clarification run emitting multiple questions."""

    @pytest.mark.asyncio
    async def test_emits_questions_for_all_missing_inputs(self) -> None:
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        events = await run_clarification(ctx, emitter, session_id="s1")

        assert len(events) == len(REQUIRED_INPUTS)
        for event in events:
            assert event.type == EventType.QUESTION

    @pytest.mark.asyncio
    async def test_respects_max_questions_limit(self) -> None:
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        events = await run_clarification(ctx, emitter, session_id="s1", max_questions=1)

        # Only 1 question emitted even though multiple inputs are missing
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_no_questions_when_all_inputs_present(self) -> None:
        """Requirement 3.5: proceed without questions when all present."""
        emitter = FakeEventEmitter()
        ctx = _full_context()

        events = await run_clarification(ctx, emitter, session_id="s1")

        assert events == []
        assert len(emitter.events) == 0

    @pytest.mark.asyncio
    async def test_default_max_questions_is_five(self) -> None:
        assert DEFAULT_MAX_QUESTIONS == 5


# ---------------------------------------------------------------------------
# Requirement 3.2: Record answers into SessionContext
# ---------------------------------------------------------------------------


class TestRecordAnswer:
    """Test recording answers into the SessionContext."""

    def test_records_answer_into_correct_field(self) -> None:
        ctx = _empty_context()
        result = record_answer(ctx, "goal_refinement", "Build a user auth system")
        assert result is True
        assert "Build a user auth system" in ctx.goals

    def test_records_constraints_into_constraints_field(self) -> None:
        ctx = _empty_context()
        result = record_answer(ctx, "constraints", "Must use PostgreSQL")
        assert result is True
        assert "Must use PostgreSQL" in ctx.constraints

    def test_records_preferences_into_preferences_field(self) -> None:
        ctx = _empty_context()
        result = record_answer(ctx, "tech_stack_preferences", "Use FastAPI")
        assert result is True
        assert "Use FastAPI" in ctx.preferences

    def test_strips_whitespace_from_answer(self) -> None:
        ctx = _empty_context()
        record_answer(ctx, "goal_refinement", "  spaced answer  ")
        assert ctx.goals == ["spaced answer"]

    def test_unknown_input_name_recorded_as_constraint(self) -> None:
        ctx = _empty_context()
        result = record_answer(ctx, "unknown_input", "Some value")
        assert result is True
        assert "Some value" in ctx.constraints

    def test_multiple_answers_accumulate(self) -> None:
        ctx = _empty_context()
        record_answer(ctx, "goal_refinement", "First goal")
        record_answer(ctx, "goal_refinement", "Second goal")
        assert len(ctx.goals) == 2
        assert "First goal" in ctx.goals
        assert "Second goal" in ctx.goals


# ---------------------------------------------------------------------------
# Requirement 3.6: Reject empty answers; re-emit question
# ---------------------------------------------------------------------------


class TestEmptyAnswerRejection:
    """Test that empty answers are rejected and questions re-emitted."""

    def test_empty_string_is_rejected(self) -> None:
        ctx = _empty_context()
        result = record_answer(ctx, "goal_refinement", "")
        assert result is False
        assert ctx.goals == []

    def test_whitespace_only_is_rejected(self) -> None:
        ctx = _empty_context()
        result = record_answer(ctx, "goal_refinement", "   ")
        assert result is False
        assert ctx.goals == []

    def test_none_like_empty_is_rejected(self) -> None:
        ctx = _empty_context()
        result = record_answer(ctx, "goal_refinement", "")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_answer_re_emits_question_on_empty(self) -> None:
        """Requirement 3.6: re-emit question on empty answer."""
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        can_advance, re_emitted = await handle_answer(
            ctx,
            "goal_refinement",
            "",
            emitter,
            session_id="s1",
        )

        assert can_advance is False
        assert re_emitted is not None
        assert re_emitted.type == EventType.QUESTION
        assert re_emitted.payload["input_name"] == "goal_refinement"

    @pytest.mark.asyncio
    async def test_handle_answer_does_not_advance_on_empty(self) -> None:
        """Requirement 3.6: do not advance workflow on empty answer."""
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        can_advance, _ = await handle_answer(
            ctx,
            "constraints",
            "   ",
            emitter,
            session_id="s1",
        )

        assert can_advance is False
        assert ctx.constraints == []


# ---------------------------------------------------------------------------
# Requirement 3.4: Include recorded constraints in subsequent prompts
# ---------------------------------------------------------------------------


class TestConstraintsInPrompts:
    """Test that recorded constraints appear in serialized prompts."""

    def test_recorded_constraint_appears_in_prompt(self) -> None:
        ctx = _empty_context()
        record_answer(ctx, "constraints", "Must use PostgreSQL")

        prompt = ctx.serialize_to_prompt()
        assert "Must use PostgreSQL" in prompt
        assert "Constraints:" in prompt

    def test_all_recorded_answers_appear_in_prompt(self) -> None:
        ctx = _empty_context()
        record_answer(ctx, "goal_refinement", "Build REST API")
        record_answer(ctx, "constraints", "No ORM allowed")
        record_answer(ctx, "tech_stack_preferences", "Use FastAPI")

        prompt = ctx.serialize_to_prompt()
        assert "Build REST API" in prompt
        assert "No ORM allowed" in prompt
        assert "Use FastAPI" in prompt

    def test_constraints_persist_across_serializations(self) -> None:
        """Once recorded, constraints appear in ALL subsequent serializations."""
        ctx = _empty_context()
        record_answer(ctx, "constraints", "Must use Python 3.11")

        # Serialize multiple times — constraint always present
        for _ in range(10):
            prompt = ctx.serialize_to_prompt()
            assert "Must use Python 3.11" in prompt


# ---------------------------------------------------------------------------
# Requirement 3.5: Advance when all inputs present
# ---------------------------------------------------------------------------


class TestAdvanceIfReady:
    """Test the advance_if_ready check."""

    def test_not_ready_when_context_empty(self) -> None:
        ctx = _empty_context()
        assert advance_if_ready(ctx) is False

    def test_ready_when_all_inputs_present(self) -> None:
        ctx = _full_context()
        assert advance_if_ready(ctx) is True

    def test_not_ready_when_partially_filled(self) -> None:
        ctx = _partial_context()
        assert advance_if_ready(ctx) is False

    def test_custom_inputs_check(self) -> None:
        custom_inputs = [
            SpecificationInput(
                name="only_goals",
                question="What?",
                context_field="goals",
            ),
        ]
        ctx = SessionContext(session_id="s1", goals=["something"])
        assert advance_if_ready(ctx, required_inputs=custom_inputs) is True

    @pytest.mark.asyncio
    async def test_handle_answer_returns_advance_true_when_ready(self) -> None:
        """After answering all questions, handle_answer signals advance."""
        emitter = FakeEventEmitter()
        ctx = SessionContext(
            session_id="s1",
            goals=["Already have goals"],
            constraints=["Already have constraints"],
        )

        # Only missing preferences
        can_advance, re_emitted = await handle_answer(
            ctx,
            "tech_stack_preferences",
            "Use React",
            emitter,
            session_id="s1",
        )

        assert can_advance is True
        assert re_emitted is None

    @pytest.mark.asyncio
    async def test_handle_answer_returns_advance_false_when_not_ready(self) -> None:
        """After answering one question with more remaining, don't advance."""
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        can_advance, re_emitted = await handle_answer(
            ctx,
            "goal_refinement",
            "Build an API",
            emitter,
            session_id="s1",
        )

        assert can_advance is False
        assert re_emitted is None


# ---------------------------------------------------------------------------
# Integration: full workflow
# ---------------------------------------------------------------------------


class TestFullClarificationWorkflow:
    """Integration test for the complete clarification workflow."""

    @pytest.mark.asyncio
    async def test_complete_workflow_empty_to_ready(self) -> None:
        """Simulate a full clarification flow from empty to ready."""
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        # 1. Run clarification — should emit questions for all missing inputs
        questions = await run_clarification(ctx, emitter, session_id="s1")
        assert len(questions) > 0

        # 2. Answer each question
        for spec_input in REQUIRED_INPUTS:
            can_advance, _ = await handle_answer(
                ctx,
                spec_input.name,
                f"Answer for {spec_input.name}",
                emitter,
                session_id="s1",
            )

        # 3. After answering all, should be ready to advance
        assert advance_if_ready(ctx) is True

    @pytest.mark.asyncio
    async def test_run_clarification_after_partial_answers(self) -> None:
        """After partial answers, only remaining questions are emitted."""
        emitter = FakeEventEmitter()
        ctx = _partial_context()  # goals filled, constraints + preferences missing

        questions = await run_clarification(ctx, emitter, session_id="s1")

        # Should only ask about constraints and preferences
        input_names = [q.payload["input_name"] for q in questions]
        assert "goal_refinement" not in input_names
        assert "constraints" in input_names
        assert "tech_stack_preferences" in input_names

    @pytest.mark.asyncio
    async def test_max_questions_configurable(self) -> None:
        """The max_questions parameter is respected."""
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        # With max_questions=2, only 2 questions emitted
        questions = await run_clarification(ctx, emitter, session_id="s1", max_questions=2)
        assert len(questions) == 2

    @pytest.mark.asyncio
    async def test_serialization_deterministic_after_answers(self) -> None:
        """After recording answers, serialization remains deterministic."""
        ctx = _empty_context()
        record_answer(ctx, "goal_refinement", "Build API")
        record_answer(ctx, "constraints", "Use Python")
        record_answer(ctx, "tech_stack_preferences", "FastAPI")

        first = ctx.serialize_to_prompt()
        for _ in range(50):
            assert ctx.serialize_to_prompt() == first


# ---------------------------------------------------------------------------
# Additional gap coverage
# ---------------------------------------------------------------------------


class TestHandleAnswerEdgeCases:
    """Test edge cases in handle_answer not covered above."""

    @pytest.mark.asyncio
    async def test_handle_answer_unknown_input_empty_answer(self) -> None:
        """Empty answer for unknown input name: cannot re-emit, returns (False, None)."""
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        can_advance, re_emitted = await handle_answer(
            ctx,
            "nonexistent_input",
            "",
            emitter,
            session_id="s1",
        )

        assert can_advance is False
        assert re_emitted is None
        # No events emitted and no context modified
        assert len(emitter.events) == 0
        assert ctx.constraints == []

    @pytest.mark.asyncio
    async def test_handle_answer_whitespace_only_for_known_input(self) -> None:
        """Whitespace-only answer re-emits question for known input (Req 3.6)."""
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        can_advance, re_emitted = await handle_answer(
            ctx,
            "constraints",
            "   \t\n  ",
            emitter,
            session_id="s1",
        )

        assert can_advance is False
        assert re_emitted is not None
        assert re_emitted.payload["input_name"] == "constraints"
        assert re_emitted.payload["requires_answer"] is True


class TestMaxQuestionsExceedsInputs:
    """Test max_questions behavior when there are more missing inputs than max."""

    @pytest.mark.asyncio
    async def test_max_questions_caps_emitted_questions(self) -> None:
        """When max_questions is less than missing inputs, only max are emitted."""
        # Create custom inputs list with 7 items to exceed DEFAULT_MAX_QUESTIONS
        custom_inputs = [
            SpecificationInput(name=f"input_{i}", question=f"Q{i}?", context_field="goals")
            for i in range(7)
        ]
        emitter = FakeEventEmitter()
        ctx = _empty_context()

        events = await run_clarification(
            ctx, emitter, session_id="s1",
            max_questions=5, required_inputs=custom_inputs,
        )

        assert len(events) == 5  # Capped at max_questions

    @pytest.mark.asyncio
    async def test_max_questions_greater_than_missing_emits_all(self) -> None:
        """When max_questions exceeds missing inputs, all missing are emitted."""
        emitter = FakeEventEmitter()
        ctx = _partial_context()  # only 2 inputs missing

        events = await run_clarification(
            ctx, emitter, session_id="s1", max_questions=10,
        )

        # Only 2 are actually missing (constraints, preferences)
        assert len(events) == 2


class TestConstraintPersistenceAcrossNodes:
    """Test that recorded constraints persist in context across serializations (Req 3.4)."""

    def test_constraint_survives_additional_answer_recording(self) -> None:
        """A constraint recorded early must persist after recording later answers."""
        ctx = _empty_context()
        record_answer(ctx, "constraints", "Must use PostgreSQL")

        # Record additional answers
        record_answer(ctx, "goal_refinement", "Build user auth")
        record_answer(ctx, "tech_stack_preferences", "FastAPI")

        # The constraint must still be present in every serialization
        prompt = ctx.serialize_to_prompt()
        assert "Must use PostgreSQL" in prompt
        assert "Build user auth" in prompt
        assert "FastAPI" in prompt
