"""Unit tests for the specification generation module.

Tests the SpecificationGenerator class, error handling, event emission,
and the get_specification retrieval function.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7
"""

from __future__ import annotations

import json
import pytest

from app.runtime.events.models import Event, EventType
from app.runtime.models import Role
from app.runtime.router import ModelUnavailableError, AttemptRecord
from app.runtime.specification import (
    ArtifactSaveError,
    SpecificationArtifact,
    SpecificationGenerationError,
    SpecificationGenerator,
    SpecificationNotFoundError,
    clear_all_specifications,
    clear_specification,
    get_specification,
    store_specification,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_spec_store():
    """Clear the specification store before each test."""
    clear_all_specifications()
    yield
    clear_all_specifications()


class FakeEventCollector:
    """Collects emitted events for test assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def get_by_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


def make_valid_response(
    spec_content: str = "# Test Specification\nThis is a test spec.",
    tasks: list[dict] | None = None,
) -> str:
    """Create a valid JSON response from the Architect role."""
    if tasks is None:
        tasks = [
            {"id": "task_1", "title": "Setup project", "description": "Init repo", "depends_on": []},
            {"id": "task_2", "title": "Implement core", "description": "Build core", "depends_on": ["task_1"]},
        ]
    return json.dumps({"specification": spec_content, "tasks": tasks})


def make_session_context() -> dict:
    """Create a sample session context dict."""
    return {
        "goals": ["Build a REST API"],
        "decisions": [],
        "assumptions": ["Python 3.11+"],
        "constraints": ["Must use FastAPI"],
        "preferences": ["Type hints everywhere"],
    }


def make_digital_twin() -> dict:
    """Create a sample digital twin dict."""
    return {
        "language": "python",
        "framework": "fastapi",
        "git_summary": "Initial commit with project scaffold",
        "entry_points": ["app/main.py"],
    }


# ---------------------------------------------------------------------------
# Tests: Successful generation (Requirements 4.1, 4.2, 4.3)
# ---------------------------------------------------------------------------


class TestSpecificationGeneratorSuccess:
    """Tests for successful specification generation."""

    @pytest.fixture
    def events(self) -> FakeEventCollector:
        return FakeEventCollector()

    @pytest.fixture
    def generator(self, events: FakeEventCollector) -> SpecificationGenerator:
        """Create a generator with a successful model invoke and artifact store."""

        async def mock_invoke(role, messages, **kwargs):
            assert role == Role.ARCHITECT
            return make_valid_response()

        async def mock_save(session_id, artifact_type, content):
            return f"artifact://{session_id}/{artifact_type}/v1"

        return SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

    async def test_invokes_architect_role_exactly_once(self, events: FakeEventCollector):
        """Req 4.1: Invoke the Architect role exactly once."""
        invocations = []

        async def mock_invoke(role, messages, **kwargs):
            invocations.append((role, messages))
            return make_valid_response()

        async def mock_save(session_id, artifact_type, content):
            return f"artifact://{session_id}/{artifact_type}/v1"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        await gen.generate(
            make_session_context(),
            make_digital_twin(),
            session_id="sess_1",
        )

        assert len(invocations) == 1
        assert invocations[0][0] == Role.ARCHITECT

    async def test_returns_specification_artifact(self, generator, events):
        """Req 4.2: Generate and return a specification artifact."""
        artifact = await generator.generate(
            make_session_context(),
            make_digital_twin(),
            session_id="sess_1",
        )

        assert isinstance(artifact, SpecificationArtifact)
        assert artifact.session_id == "sess_1"
        assert artifact.uri == "artifact://sess_1/specification/v1"
        assert artifact.version == 1
        assert artifact.content == "# Test Specification\nThis is a test spec."
        assert artifact.task_count == 2
        assert len(artifact.task_list) == 2

    async def test_emits_spec_ready_event(self, generator, events):
        """Req 4.2: Emit spec.ready event with URI and version."""
        await generator.generate(
            make_session_context(),
            make_digital_twin(),
            session_id="sess_1",
        )

        spec_events = events.get_by_type(EventType.SPEC_READY)
        assert len(spec_events) == 1
        event = spec_events[0]
        assert event.payload["uri"] == "artifact://sess_1/specification/v1"
        assert event.payload["version"] == 1
        assert event.session_id == "sess_1"
        assert event.source == "specification"

    async def test_emits_tasks_ready_event(self, generator, events):
        """Req 4.3: Emit tasks.ready event with task count and graph reference."""
        await generator.generate(
            make_session_context(),
            make_digital_twin(),
            session_id="sess_1",
        )

        tasks_events = events.get_by_type(EventType.TASKS_READY)
        assert len(tasks_events) == 1
        event = tasks_events[0]
        assert event.payload["task_count"] == 2
        assert "graph_ref" in event.payload
        assert event.payload["graph_ref"].startswith("task_graph:sess_1:")

    async def test_version_monotonically_increases(self, events):
        """Req 4.2: Version is unique and strictly increasing per session."""

        async def mock_invoke(role, messages, **kwargs):
            return make_valid_response()

        async def mock_save(session_id, artifact_type, content):
            return f"artifact://{session_id}/{artifact_type}/latest"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        art1 = await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")
        art2 = await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")
        art3 = await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")

        assert art1.version == 1
        assert art2.version == 2
        assert art3.version == 3

    async def test_versions_independent_per_session(self, events):
        """Version tracking is per-session, not global."""

        async def mock_invoke(role, messages, **kwargs):
            return make_valid_response()

        async def mock_save(session_id, artifact_type, content):
            return f"artifact://{session_id}/{artifact_type}/latest"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        art_a = await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_a")
        art_b = await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_b")

        assert art_a.version == 1
        assert art_b.version == 1

    async def test_passes_context_to_messages(self, events):
        """The session context is passed to the model invocation messages."""
        captured_messages = []

        async def mock_invoke(role, messages, **kwargs):
            captured_messages.append(messages)
            return make_valid_response()

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        ctx = make_session_context()
        twin = make_digital_twin()
        await gen.generate(ctx, twin, session_id="sess_1")

        assert len(captured_messages) == 1
        msgs = captured_messages[0]
        # Should have system and user messages
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        # User message should contain context info
        assert "Build a REST API" in msgs[1]["content"]
        assert "python" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# Tests: Error handling — ModelRouter failure (Requirement 4.5)
# ---------------------------------------------------------------------------


class TestSpecificationGeneratorModelFailure:
    """Tests for Architect role invocation failures."""

    @pytest.fixture
    def events(self) -> FakeEventCollector:
        return FakeEventCollector()

    async def test_model_unavailable_emits_error_event(self, events):
        """Req 4.5: On ModelUnavailableError, emit error event."""

        async def mock_invoke(role, messages, **kwargs):
            raise ModelUnavailableError(
                role=Role.ARCHITECT,
                attempt_log=[AttemptRecord(provider="openai", model="gpt-4", status="failed")],
            )

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        with pytest.raises(SpecificationGenerationError):
            await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")

        error_events = events.get_by_type(EventType.ERROR)
        assert len(error_events) == 1
        assert error_events[0].payload["error_type"] == "generation_failure"
        assert "Architect role unavailable" in error_events[0].payload["detail"]

    async def test_model_unavailable_does_not_emit_spec_ready(self, events):
        """Req 4.5: Do not advance (no spec.ready emitted) on failure."""

        async def mock_invoke(role, messages, **kwargs):
            raise ModelUnavailableError(
                role=Role.ARCHITECT,
                attempt_log=[],
            )

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        with pytest.raises(SpecificationGenerationError):
            await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")

        assert len(events.get_by_type(EventType.SPEC_READY)) == 0
        assert len(events.get_by_type(EventType.TASKS_READY)) == 0

    async def test_generic_exception_emits_error_event(self, events):
        """Req 4.5: Any generation failure emits an error event."""

        async def mock_invoke(role, messages, **kwargs):
            raise RuntimeError("Network timeout")

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        with pytest.raises(SpecificationGenerationError):
            await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")

        error_events = events.get_by_type(EventType.ERROR)
        assert len(error_events) == 1
        assert "Network timeout" in error_events[0].payload["detail"]

    async def test_empty_response_emits_error_event(self, events):
        """Req 4.5: Empty Architect response emits error event."""

        async def mock_invoke(role, messages, **kwargs):
            return ""

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        with pytest.raises(SpecificationGenerationError):
            await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")

        error_events = events.get_by_type(EventType.ERROR)
        assert len(error_events) == 1


# ---------------------------------------------------------------------------
# Tests: Error handling — Artifact save failure (Requirement 4.6)
# ---------------------------------------------------------------------------


class TestSpecificationGeneratorSaveFailure:
    """Tests for artifact store save failures."""

    @pytest.fixture
    def events(self) -> FakeEventCollector:
        return FakeEventCollector()

    async def test_save_failure_emits_error_event(self, events):
        """Req 4.6: On save failure, emit error event."""

        async def mock_invoke(role, messages, **kwargs):
            return make_valid_response()

        async def mock_save(session_id, artifact_type, content):
            raise IOError("Disk full")

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        with pytest.raises(ArtifactSaveError):
            await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")

        error_events = events.get_by_type(EventType.ERROR)
        assert len(error_events) == 1
        assert error_events[0].payload["error_type"] == "save_failure"
        assert "Disk full" in error_events[0].payload["detail"]

    async def test_save_failure_does_not_emit_spec_ready(self, events):
        """Req 4.6: Do not emit spec.ready on save failure."""

        async def mock_invoke(role, messages, **kwargs):
            return make_valid_response()

        async def mock_save(session_id, artifact_type, content):
            raise IOError("Connection refused")

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        with pytest.raises(ArtifactSaveError):
            await gen.generate(make_session_context(), make_digital_twin(), session_id="sess_1")

        assert len(events.get_by_type(EventType.SPEC_READY)) == 0
        assert len(events.get_by_type(EventType.TASKS_READY)) == 0


# ---------------------------------------------------------------------------
# Tests: Specification retrieval (Requirements 4.4, 4.7)
# ---------------------------------------------------------------------------


class TestGetSpecification:
    """Tests for get_specification retrieval function."""

    def test_returns_stored_specification(self):
        """Req 4.4: Return the most recent specification artifact."""
        artifact = SpecificationArtifact(
            session_id="sess_1",
            uri="artifact://sess_1/specification/v1",
            version=1,
            content="Test spec",
            task_list=[{"id": "t1", "title": "Task 1"}],
            task_count=1,
            graph_ref="task_graph:sess_1:v1",
        )
        store_specification(artifact)

        result = get_specification("sess_1")
        assert result.session_id == "sess_1"
        assert result.uri == "artifact://sess_1/specification/v1"
        assert result.version == 1

    def test_not_found_for_missing_session(self):
        """Req 4.7: Return not-found for session with no specification."""
        with pytest.raises(SpecificationNotFoundError) as exc_info:
            get_specification("nonexistent_session")

        assert exc_info.value.session_id == "nonexistent_session"
        assert "nonexistent_session" in str(exc_info.value)

    def test_returns_most_recent_version(self):
        """Req 4.4: Return the most recent specification artifact."""
        art_v1 = SpecificationArtifact(
            session_id="sess_1", uri="v1_uri", version=1, content="v1"
        )
        art_v2 = SpecificationArtifact(
            session_id="sess_1", uri="v2_uri", version=2, content="v2"
        )

        store_specification(art_v1)
        store_specification(art_v2)

        result = get_specification("sess_1")
        assert result.version == 2
        assert result.uri == "v2_uri"

    def test_clear_specification(self):
        """Clearing a session's spec causes not-found on retrieval."""
        artifact = SpecificationArtifact(session_id="sess_1", uri="test", version=1)
        store_specification(artifact)

        clear_specification("sess_1")

        with pytest.raises(SpecificationNotFoundError):
            get_specification("sess_1")

    def test_clear_nonexistent_session_is_safe(self):
        """Clearing a nonexistent session does not raise."""
        clear_specification("nonexistent")  # Should not raise


# ---------------------------------------------------------------------------
# Tests: Response parsing edge cases
# ---------------------------------------------------------------------------


class TestResponseParsing:
    """Tests for Architect response parsing."""

    @pytest.fixture
    def events(self) -> FakeEventCollector:
        return FakeEventCollector()

    async def test_parses_valid_json_response(self, events):
        """Valid JSON with specification and tasks fields."""
        response = json.dumps({
            "specification": "My spec content",
            "tasks": [
                {"id": "t1", "title": "First task", "description": "Do first", "depends_on": []},
            ],
        })

        async def mock_invoke(role, messages, **kwargs):
            return response

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        artifact = await gen.generate(make_session_context(), make_digital_twin(), session_id="s1")
        assert artifact.content == "My spec content"
        assert artifact.task_count == 1
        assert artifact.task_list[0]["id"] == "t1"

    async def test_non_json_response_treated_as_spec_content(self, events):
        """Non-JSON response is treated as the specification content."""
        raw_spec = "# My Spec\n\nThis is a free-form specification."

        async def mock_invoke(role, messages, **kwargs):
            return raw_spec

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        artifact = await gen.generate(make_session_context(), make_digital_twin(), session_id="s1")
        assert artifact.content == raw_spec
        assert artifact.task_count == 1
        assert artifact.task_list[0]["id"] == "task_1"

    async def test_json_without_specification_field_raises(self, events):
        """JSON missing 'specification' field raises error."""
        response = json.dumps({"tasks": [{"id": "t1"}]})

        async def mock_invoke(role, messages, **kwargs):
            return response

        async def mock_save(session_id, artifact_type, content):
            return "uri://test"

        gen = SpecificationGenerator(
            model_invoke=mock_invoke,
            artifact_store=mock_save,
            event_emitter=events.emit,
        )

        with pytest.raises(SpecificationGenerationError):
            await gen.generate(make_session_context(), make_digital_twin(), session_id="s1")
