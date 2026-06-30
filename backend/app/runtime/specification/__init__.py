"""Specification and task generation via the Architect role.

Implements the specification phase of the build workflow: invokes the Architect
role exactly once after clarification completes, generates a specification
artifact and task list from the Digital Twin + SessionContext, saves the artifact
to the Artifact Store, and emits `spec.ready` and `tasks.ready` events.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType
from app.runtime.models import Role
from app.runtime.router import ModelUnavailableError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# An event emitter is an async callable that publishes an Event.
EventEmitter = Callable[[Event], Awaitable[Event]]

# An artifact store save function: (session_id, artifact_type, content) -> URI
ArtifactSaveFunc = Callable[[str, str, str], Awaitable[str]]

# A model router invoke function: (role, messages, **kwargs) -> completion string
ModelInvokeFunc = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SpecificationArtifact:
    """A generated specification artifact from the Architect role.

    Attributes:
        session_id: The session this artifact belongs to.
        uri: The artifact store URI after save.
        version: Monotonically increasing version per session.
        content: The raw specification content.
        task_list: The list of tasks extracted from the generation.
        task_count: Number of tasks in the task list.
        graph_ref: Reference to the task dependency graph.
    """

    session_id: str
    uri: str = ""
    version: int = 0
    content: str = ""
    task_list: list[dict[str, Any]] = field(default_factory=list)
    task_count: int = 0
    graph_ref: str = ""


class SpecificationNotFoundError(Exception):
    """Raised when no specification exists for a session.

    Per Requirement 4.7: return a not-found response identifying the session.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"No specification artifact exists for session '{session_id}'"
        )


class SpecificationGenerationError(Exception):
    """Raised when the Architect role fails to generate a specification.

    Per Requirement 4.5: emit error event, do not advance to planning.
    """

    def __init__(self, message: str, *, session_id: str = "") -> None:
        self.session_id = session_id
        super().__init__(message)


class ArtifactSaveError(Exception):
    """Raised when saving the artifact to the Artifact Store fails.

    Per Requirement 4.6: do not emit spec.ready, emit error event.
    """

    def __init__(self, message: str, *, session_id: str = "") -> None:
        self.session_id = session_id
        super().__init__(message)


# ---------------------------------------------------------------------------
# SpecificationGenerator
# ---------------------------------------------------------------------------


class SpecificationGenerator:
    """Generates specification artifacts and task lists via the Architect role.

    Orchestrates the specification phase:
    1. Invokes the Architect role exactly once via ModelRouter (Req 4.1)
    2. Parses the response into a specification artifact + task list
    3. Saves the artifact to the artifact store (Req 4.2)
    4. Emits `spec.ready` event with URI and version (Req 4.2)
    5. Emits `tasks.ready` event with task count and graph reference (Req 4.3)

    Error handling:
    - On ModelRouter failure (ModelUnavailableError): emits error event,
      does not advance (Req 4.5)
    - On artifact save failure: does not emit spec.ready, emits error
      event (Req 4.6)

    Requirements: 4.1, 4.2, 4.3, 4.5, 4.6
    """

    def __init__(
        self,
        *,
        model_invoke: ModelInvokeFunc,
        artifact_store: ArtifactSaveFunc,
        event_emitter: EventEmitter,
    ) -> None:
        """Initialize the SpecificationGenerator.

        Args:
            model_invoke: Async callable that invokes a model role.
                Signature: (role, messages, **kwargs) -> str
            artifact_store: Async callable that saves an artifact.
                Signature: (session_id, artifact_type, content) -> URI
            event_emitter: Async callable that publishes an Event.
        """
        self._model_invoke = model_invoke
        self._artifact_store = artifact_store
        self._event_emitter = event_emitter

        # Per-session version tracking (monotonically increasing)
        self._versions: dict[str, int] = defaultdict(int)

    async def generate(
        self,
        session_context: dict[str, Any],
        digital_twin: dict[str, Any],
        *,
        session_id: str = "",
    ) -> SpecificationArtifact:
        """Generate a specification artifact and task list from context and twin.

        Invokes the Architect role exactly once (Req 4.1), parses the response,
        saves to the artifact store, and emits spec.ready + tasks.ready events.

        Args:
            session_context: The SessionContext serialized as a dict (goals,
                decisions, assumptions, constraints, preferences).
            digital_twin: The Digital Twin data as a dict (language, framework,
                file_index, git_summary, entry_points, etc.).
            session_id: The session identifier.

        Returns:
            The generated SpecificationArtifact with URI and version set.

        Raises:
            SpecificationGenerationError: If the Architect role fails to
                produce a valid specification (after emitting error event).
        """
        # Step 1: Invoke Architect role exactly once (Req 4.1)
        messages = self._build_messages(session_context, digital_twin)

        try:
            completion = await self._model_invoke(
                Role.ARCHITECT, messages, estimated_tokens=4000
            )
        except ModelUnavailableError as exc:
            # Req 4.5: emit error event, do not advance
            await self._emit_error(
                session_id=session_id,
                error_type="generation_failure",
                detail=f"Architect role unavailable: {exc}",
            )
            raise SpecificationGenerationError(
                f"Architect role unavailable: {exc}",
                session_id=session_id,
            ) from exc
        except Exception as exc:
            # Req 4.5: emit error event for any generation failure
            await self._emit_error(
                session_id=session_id,
                error_type="generation_failure",
                detail=f"Architect invocation failed: {exc}",
            )
            raise SpecificationGenerationError(
                f"Architect invocation failed: {exc}",
                session_id=session_id,
            ) from exc

        # Step 2: Parse the response into specification + task list
        try:
            spec_content, task_list = self._parse_response(completion)
        except Exception as exc:
            # Req 4.5: emit error event if parsing fails
            await self._emit_error(
                session_id=session_id,
                error_type="generation_failure",
                detail=f"Failed to parse Architect response: {exc}",
            )
            raise SpecificationGenerationError(
                f"Failed to parse Architect response: {exc}",
                session_id=session_id,
            ) from exc

        # Step 3: Save artifact to the artifact store (Req 4.2)
        try:
            uri = await self._artifact_store(session_id, "specification", spec_content)
        except Exception as exc:
            # Req 4.6: do not emit spec.ready, emit error event
            await self._emit_error(
                session_id=session_id,
                error_type="save_failure",
                detail=f"Failed to save specification artifact: {exc}",
            )
            raise ArtifactSaveError(
                f"Failed to save specification artifact: {exc}",
                session_id=session_id,
            ) from exc

        # Bump version (monotonically increasing per session, Req 4.2)
        self._versions[session_id] += 1
        version = self._versions[session_id]

        # Build the graph reference
        graph_ref = f"task_graph:{session_id}:v{version}"

        # Create the artifact object
        artifact = SpecificationArtifact(
            session_id=session_id,
            uri=uri,
            version=version,
            content=spec_content,
            task_list=task_list,
            task_count=len(task_list),
            graph_ref=graph_ref,
        )

        # Step 4: Emit spec.ready event (Req 4.2)
        await self._emit_spec_ready(session_id, uri, version)

        # Step 5: Emit tasks.ready event (Req 4.3)
        await self._emit_tasks_ready(session_id, len(task_list), graph_ref)

        return artifact

    def _build_messages(
        self,
        session_context: dict[str, Any],
        digital_twin: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build the messages list for the Architect role invocation.

        Constructs a system prompt and user prompt from the session context
        and digital twin data.

        Args:
            session_context: The serialized session context.
            digital_twin: The digital twin data.

        Returns:
            A list of message dicts suitable for model invocation.
        """
        # Build context section from session_context
        context_parts: list[str] = []
        for field_name in ("goals", "decisions", "assumptions", "constraints", "preferences"):
            items = session_context.get(field_name, [])
            if items:
                title = field_name.capitalize()
                lines = "\n".join(f"- {item}" for item in items)
                context_parts.append(f"{title}:\n{lines}")

        context_text = "\n\n".join(context_parts) if context_parts else "No context provided."

        # Build twin section
        twin_parts: list[str] = []
        if digital_twin.get("language"):
            twin_parts.append(f"Language: {digital_twin['language']}")
        if digital_twin.get("framework"):
            twin_parts.append(f"Framework: {digital_twin['framework']}")
        if digital_twin.get("git_summary"):
            twin_parts.append(f"Git summary: {digital_twin['git_summary']}")
        if digital_twin.get("entry_points"):
            twin_parts.append(
                f"Entry points: {', '.join(digital_twin['entry_points'])}"
            )

        twin_text = "\n".join(twin_parts) if twin_parts else "No repository information."

        system_prompt = (
            "You are the Architect role in the Forge build system. "
            "Generate a specification and task list for the given project context. "
            "Return your response as JSON with two keys: "
            "'specification' (a string with the full spec) and "
            "'tasks' (a list of task objects with 'id', 'title', 'description', "
            "and 'depends_on' fields)."
        )

        user_prompt = (
            f"## Session Context\n\n{context_text}\n\n"
            f"## Repository Information\n\n{twin_text}\n\n"
            "Generate a specification artifact and a task list for this project."
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _parse_response(
        self, completion: str
    ) -> tuple[str, list[dict[str, Any]]]:
        """Parse the Architect role's response into spec content and task list.

        Attempts JSON parsing first. Falls back to treating the whole response
        as a specification with a single task if JSON parsing fails.

        Args:
            completion: The raw completion string from the Architect role.

        Returns:
            A tuple of (specification_content, task_list).

        Raises:
            ValueError: If the response cannot be parsed at all.
        """
        if not completion or not completion.strip():
            raise ValueError("Empty response from Architect role")

        # Try JSON parsing first
        try:
            data = json.loads(completion)
            spec_content = data.get("specification", "")
            task_list = data.get("tasks", [])

            if not spec_content:
                raise ValueError("Missing 'specification' field in response")
            if not isinstance(task_list, list):
                raise ValueError("'tasks' field must be a list")

            return spec_content, task_list
        except (json.JSONDecodeError, TypeError):
            # Fall back: treat the whole response as a specification
            # with an implicit single implementation task
            return completion, [
                {
                    "id": "task_1",
                    "title": "Implement specification",
                    "description": "Implement the specification as described",
                    "depends_on": [],
                }
            ]

    async def _emit_spec_ready(
        self, session_id: str, uri: str, version: int
    ) -> None:
        """Emit a spec.ready event with URI and version.

        Per Requirement 4.2: emit spec.ready with artifact URI and a version
        that is unique and strictly increasing per session.

        Args:
            session_id: The session identifier.
            uri: The artifact store URI.
            version: The version number (monotonically increasing per session).
        """
        event = Event.create(
            type=EventType.SPEC_READY,
            session_id=session_id,
            source="specification",
            payload={
                "uri": uri,
                "version": version,
            },
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await self._event_emitter(event)

    async def _emit_tasks_ready(
        self, session_id: str, task_count: int, graph_ref: str
    ) -> None:
        """Emit a tasks.ready event with task count and graph reference.

        Per Requirement 4.3: emit tasks.ready carrying the task count
        and a task graph reference.

        Args:
            session_id: The session identifier.
            task_count: Number of tasks in the task list.
            graph_ref: Reference to the task dependency graph.
        """
        event = Event.create(
            type=EventType.TASKS_READY,
            session_id=session_id,
            source="specification",
            payload={
                "task_count": task_count,
                "graph_ref": graph_ref,
            },
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await self._event_emitter(event)

    async def _emit_error(
        self, *, session_id: str, error_type: str, detail: str
    ) -> None:
        """Emit an error event for generation or save failures.

        Per Requirements 4.5/4.6: emit error event on failure, do not advance.

        Args:
            session_id: The session identifier.
            error_type: The type of error (generation_failure, save_failure).
            detail: Human-readable error detail.
        """
        event = Event.create(
            type=EventType.ERROR,
            session_id=session_id,
            source="specification",
            payload={
                "error_type": error_type,
                "detail": detail,
                "phase": "specification",
            },
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await self._event_emitter(event)


# ---------------------------------------------------------------------------
# Specification retrieval (Requirement 4.4, 4.7)
# ---------------------------------------------------------------------------

# In-memory store for specifications (per-session, most recent wins)
_specification_store: dict[str, SpecificationArtifact] = {}


def store_specification(artifact: SpecificationArtifact) -> None:
    """Store a specification artifact for later retrieval.

    Args:
        artifact: The specification artifact to store.
    """
    _specification_store[artifact.session_id] = artifact


def get_specification(session_id: str) -> SpecificationArtifact:
    """Retrieve the current specification artifact for a session.

    Per Requirement 4.4: return the most recent specification artifact.
    Per Requirement 4.7: return not-found if no artifact exists.

    Args:
        session_id: The session to retrieve the specification for.

    Returns:
        The most recent SpecificationArtifact for the session.

    Raises:
        SpecificationNotFoundError: If no specification exists for the session.
    """
    artifact = _specification_store.get(session_id)
    if artifact is None:
        raise SpecificationNotFoundError(session_id)
    return artifact


def clear_specification(session_id: str) -> None:
    """Clear the specification for a session (e.g., on session deletion).

    Args:
        session_id: The session to clear.
    """
    _specification_store.pop(session_id, None)


def clear_all_specifications() -> None:
    """Clear all stored specifications (used in testing)."""
    _specification_store.clear()
