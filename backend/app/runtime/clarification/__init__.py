"""Clarification workflow for the Forge Runtime.

Implements the clarification phase of the build workflow: identifies missing
specification inputs, emits clarification question events, records developer
answers into SessionContext, and advances when all inputs are satisfied.

The SessionContext uses a fixed declared field order for deterministic
serialization into prompts — same content always produces the same output.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.runtime.events.models import Event, EventType


# ---------------------------------------------------------------------------
# SessionContext — deterministic prompt-constructing context
# ---------------------------------------------------------------------------

# The field order is declared and fixed. Serialization always follows this order
# so that identical context produces identical prompt text (Requirement 3.3).
_FIELD_ORDER = ("goals", "decisions", "assumptions", "constraints", "preferences")


@dataclass
class SessionContext:
    """Persistent per-session working memory for the clarification workflow.

    Fields are declared in a fixed order for deterministic serialization.
    The `serialize_to_prompt()` method produces identical output for identical
    content, regardless of insertion order of items.

    Requirement 3.3: fixed declared field order that produces identical
    serialized output for identical context.
    """

    session_id: str = ""
    goals: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)

    def serialize_to_prompt(self) -> str:
        """Deterministic serialization of context into a prompt string.

        Produces identical output for identical context — same content always
        generates the same string, ensuring reproducible prompts.

        The field order is fixed and declared: goals, decisions, assumptions,
        constraints, preferences (Requirement 3.3).

        Returns:
            A deterministic string representation of the session context.
            Returns empty string if all fields are empty.
        """
        sections: list[str] = []

        for field_name in _FIELD_ORDER:
            items: list[str] = getattr(self, field_name)
            if items:
                title = field_name.capitalize()
                lines = "\n".join(f"- {item}" for item in items)
                sections.append(f"{title}:\n{lines}")

        return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Required specification inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpecificationInput:
    """A required specification input that may need clarification.

    Attributes:
        name: Machine-readable identifier for the input.
        question: Human-readable clarification question to ask the developer.
        context_field: Which SessionContext field to record the answer into.
    """

    name: str
    question: str
    context_field: str


# Default required inputs for a build intent specification.
# These represent the minimum information needed to produce a good specification.
REQUIRED_INPUTS: list[SpecificationInput] = [
    SpecificationInput(
        name="goal_refinement",
        question="Can you describe more specifically what you want built? "
                 "What are the key features or behaviors?",
        context_field="goals",
    ),
    SpecificationInput(
        name="constraints",
        question="Are there any constraints I should know about? "
                 "(e.g., must use specific libraries, avoid certain patterns, "
                 "performance requirements, compatibility needs)",
        context_field="constraints",
    ),
    SpecificationInput(
        name="tech_stack_preferences",
        question="Do you have preferences for the technology stack or approach? "
                 "(e.g., preferred frameworks, languages, architecture style)",
        context_field="preferences",
    ),
]


# Default maximum number of clarification questions per build intent.
DEFAULT_MAX_QUESTIONS = 5


# ---------------------------------------------------------------------------
# Event emitter type
# ---------------------------------------------------------------------------

# An event emitter is an async callable that publishes an Event.
EventEmitter = Callable[[Event], Awaitable[Event]]


# ---------------------------------------------------------------------------
# Clarification workflow functions
# ---------------------------------------------------------------------------


def get_missing_inputs(
    context: SessionContext,
    required_inputs: list[SpecificationInput] | None = None,
) -> list[SpecificationInput]:
    """Identify which required specification inputs are still missing.

    A required input is considered present when its corresponding
    SessionContext field has at least one non-empty entry.

    Args:
        context: The current session context.
        required_inputs: Override the default required inputs list.

    Returns:
        List of SpecificationInput objects that are still missing.
    """
    inputs = required_inputs if required_inputs is not None else REQUIRED_INPUTS

    missing: list[SpecificationInput] = []
    for spec_input in inputs:
        field_items: list[str] = getattr(context, spec_input.context_field, [])
        if not field_items:
            missing.append(spec_input)

    return missing


async def emit_question(
    input_spec: SpecificationInput,
    event_emitter: EventEmitter,
    *,
    session_id: str = "",
    causation_id: str | None = None,
) -> Event:
    """Emit a clarification question event for a missing specification input.

    Per Requirement 3.1: emit a clarification question event for each missing
    input, up to the configured maximum.

    Args:
        input_spec: The specification input to ask about.
        event_emitter: Async callable to publish the event.
        session_id: The session to associate the event with.
        causation_id: Optional causation link to the triggering event.

    Returns:
        The published Event with seq assigned.
    """
    event = Event.create(
        type=EventType.QUESTION,
        session_id=session_id,
        source="clarification",
        payload={
            "input_name": input_spec.name,
            "question": input_spec.question,
            "context_field": input_spec.context_field,
            "requires_answer": True,
        },
        causation_id=causation_id,
        correlation_id=session_id,
        event_id=str(uuid.uuid4()),
    )
    return await event_emitter(event)


def record_answer(
    context: SessionContext,
    input_name: str,
    answer: str,
    *,
    required_inputs: list[SpecificationInput] | None = None,
) -> bool:
    """Record a developer's answer into the SessionContext.

    Per Requirement 3.2: record the answer and any stated constraints into
    the SessionContext before advancing the workflow.

    Per Requirement 3.6: if the answer is empty (whitespace-only), do NOT
    record it and return False to indicate the workflow should not advance.

    Args:
        context: The session context to update.
        input_name: The name of the specification input being answered.
        answer: The developer's answer text.
        required_inputs: Override the default required inputs list.

    Returns:
        True if the answer was recorded successfully.
        False if the answer was empty (workflow should re-emit question).
    """
    # Requirement 3.6: reject empty answers
    if not answer or not answer.strip():
        return False

    inputs = required_inputs if required_inputs is not None else REQUIRED_INPUTS

    # Find the matching input to determine which context field to update
    target_field: str | None = None
    for spec_input in inputs:
        if spec_input.name == input_name:
            target_field = spec_input.context_field
            break

    if target_field is None:
        # Unknown input name — record as a general constraint
        target_field = "constraints"

    # Record the answer into the appropriate context field
    field_items: list[str] = getattr(context, target_field)
    field_items.append(answer.strip())

    return True


def advance_if_ready(
    context: SessionContext,
    required_inputs: list[SpecificationInput] | None = None,
) -> bool:
    """Check if all required specification inputs are present.

    Per Requirement 3.5: when all required specification inputs are present,
    proceed without emitting a clarification question.

    Args:
        context: The current session context.
        required_inputs: Override the default required inputs list.

    Returns:
        True if all required inputs are satisfied and the workflow can advance.
        False if there are still missing inputs.
    """
    missing = get_missing_inputs(context, required_inputs)
    return len(missing) == 0


async def run_clarification(
    context: SessionContext,
    event_emitter: EventEmitter,
    *,
    session_id: str = "",
    max_questions: int = DEFAULT_MAX_QUESTIONS,
    required_inputs: list[SpecificationInput] | None = None,
    causation_id: str | None = None,
) -> list[Event]:
    """Run the clarification phase: emit questions for missing inputs.

    Emits up to `max_questions` clarification question events for missing
    specification inputs. Returns the list of emitted question events.

    Per Requirement 3.1: emit a clarification question event for each missing
    input, up to the configured maximum number of clarification questions.

    Per Requirement 3.5: if all required inputs are present, returns an empty
    list (no questions needed).

    Args:
        context: The current session context.
        event_emitter: Async callable to publish events.
        session_id: The session identifier.
        max_questions: Maximum number of questions to emit (default 5).
        required_inputs: Override the default required inputs list.
        causation_id: Optional causation link.

    Returns:
        List of emitted question Events (empty if all inputs are present).
    """
    # Requirement 3.5: proceed without questions when all inputs are present
    if advance_if_ready(context, required_inputs):
        return []

    missing = get_missing_inputs(context, required_inputs)

    # Emit up to max_questions (Requirement 3.1)
    emitted: list[Event] = []
    for spec_input in missing[:max_questions]:
        event = await emit_question(
            spec_input,
            event_emitter,
            session_id=session_id,
            causation_id=causation_id,
        )
        emitted.append(event)

    return emitted


async def handle_answer(
    context: SessionContext,
    input_name: str,
    answer: str,
    event_emitter: EventEmitter,
    *,
    session_id: str = "",
    max_questions: int = DEFAULT_MAX_QUESTIONS,
    required_inputs: list[SpecificationInput] | None = None,
    causation_id: str | None = None,
) -> tuple[bool, Event | None]:
    """Handle a developer's answer to a clarification question.

    Records the answer if non-empty. If empty, re-emits the question
    (Requirement 3.6). Returns whether the workflow can advance.

    Args:
        context: The session context to update.
        input_name: The name of the specification input being answered.
        answer: The developer's answer text.
        event_emitter: Async callable to publish events.
        session_id: The session identifier.
        max_questions: Maximum number of questions allowed.
        required_inputs: Override the default required inputs list.
        causation_id: Optional causation link.

    Returns:
        Tuple of (can_advance, re_emitted_event).
        - can_advance is True if all required inputs are now satisfied.
        - re_emitted_event is the re-emitted question Event if the answer
          was empty, or None if the answer was accepted.
    """
    inputs = required_inputs if required_inputs is not None else REQUIRED_INPUTS

    recorded = record_answer(context, input_name, answer, required_inputs=inputs)

    if not recorded:
        # Requirement 3.6: re-emit question on empty answer
        # Find the matching input spec
        target_input: SpecificationInput | None = None
        for spec_input in inputs:
            if spec_input.name == input_name:
                target_input = spec_input
                break

        if target_input is not None:
            re_emitted = await emit_question(
                target_input,
                event_emitter,
                session_id=session_id,
                causation_id=causation_id,
            )
            return False, re_emitted

        # Unknown input name — cannot re-emit; workflow does not advance
        return False, None

    # Check if we can advance now
    can_advance = advance_if_ready(context, inputs)
    return can_advance, None
