"""Unit tests for all Forge LangGraph workflow node functions.

Tests each node in isolation with mocked RuntimeDeps, verifying correct
state updates, event emissions, and error handling.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState, Task
from app.workflow.nodes.architect import make_architect_node
from app.workflow.nodes.clarify import make_clarify_node
from app.workflow.nodes.classify import make_classify_node
from app.workflow.nodes.commit import make_commit_node
from app.workflow.nodes.doc_update import make_doc_update_node
from app.workflow.nodes.execute import make_execute_node
from app.workflow.nodes.finalize import make_finalize_node
from app.workflow.nodes.intake import make_intake_node
from app.workflow.nodes.interrupt import make_interrupt_node
from app.workflow.nodes.plan import make_plan_node
from app.workflow.nodes.policy import make_policy_node
from app.workflow.nodes.status import make_status_node
from app.workflow.nodes.verify import make_verify_node


# ---------------------------------------------------------------------------
# Fixtures: mock RuntimeDeps
# ---------------------------------------------------------------------------


@dataclass
class MockWorkspaceInfo:
    workspace_id: str = "ws-001"
    task_id: str = "task_1"
    session_id: str = "sess-1"
    path: str = "/tmp/workspace"


@dataclass
class MockPausedState:
    is_paused: bool = True
    paused_at: float = 0.0
    retained_state: dict[str, Any] = field(default_factory=dict)
    interrupt_message: str = ""


class MockPolicyResult:
    def __init__(self):
        self.decision = MagicMock()
        self.decision.value = "retry"
        self.task_id = "task_1"
        self.stage = "lint"
        self.attempt_count = 1
        self.rule_matched = "default"
        self.reason = "test reason"


class MockRuntimeStatus:
    def __init__(self):
        self.current_node = "idle"


def make_mock_deps():
    """Create a mock RuntimeDeps with all required fields."""
    deps = MagicMock()

    # EventBus
    deps.event_bus = MagicMock()
    deps.event_bus.publish = AsyncMock(return_value=MagicMock(seq=1))

    # Recovery
    deps.recovery = MagicMock()
    deps.recovery.checkpoint_after_node = AsyncMock()

    # Workspace manager
    deps.workspace_manager = MagicMock()
    deps.workspace_manager.create = AsyncMock(return_value=MockWorkspaceInfo())

    # Model router
    deps.model_router = MagicMock()
    deps.model_router.route = AsyncMock(return_value='{"specification": "test spec", "tasks": [{"id": "task_1", "title": "Test Task", "description": "Do something", "depends_on": []}]}')

    # Interrupt handler
    deps.interrupt_handler = MagicMock()
    deps.interrupt_handler.pause = AsyncMock(return_value=MockPausedState())

    # Policy engine
    deps.policy_engine = MagicMock()
    deps.policy_engine.decide = AsyncMock(return_value=MockPolicyResult())

    # Inspector
    deps.inspector = MagicMock()
    deps.inspector.get_status = MagicMock(return_value=MockRuntimeStatus())

    # Learning recorder
    deps.learning_recorder = MagicMock()
    deps.learning_recorder.record_build_outcomes = AsyncMock()

    return deps


# ---------------------------------------------------------------------------
# Tests: intake_node
# ---------------------------------------------------------------------------


class TestIntakeNode:
    """Tests for the intake node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_valid_message_sets_processing(self, deps):
        """Valid message sets status to processing and appends intake to node_path."""
        node_fn = make_intake_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "Build authentication",
            "node_path": [],
            "errors": [],
        }

        result = await node_fn(state)

        assert result["status"] == "processing"
        assert "intake" in result["node_path"]

    async def test_empty_message_sets_failed(self, deps):
        """Empty message sets status to failed with invalid_input error."""
        node_fn = make_intake_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "",
            "node_path": [],
            "errors": [],
        }

        result = await node_fn(state)

        assert result["status"] == "failed"
        assert len(result["errors"]) == 1
        assert result["errors"][0]["code"] == "invalid_input"
        assert "intake" in result["node_path"]

    async def test_whitespace_message_sets_failed(self, deps):
        """Whitespace-only message is treated as empty."""
        node_fn = make_intake_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "   \t\n  ",
            "node_path": [],
            "errors": [],
        }

        result = await node_fn(state)

        assert result["status"] == "failed"
        assert result["errors"][0]["code"] == "invalid_input"

    async def test_checkpoint_written_on_success(self, deps):
        """Checkpoint is written via CrashRecovery on valid input."""
        node_fn = make_intake_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "Build something",
            "node_path": [],
            "errors": [],
        }

        await node_fn(state)

        deps.recovery.checkpoint_after_node.assert_called_once()

    async def test_missing_message_field(self, deps):
        """State with no message field is treated as invalid."""
        node_fn = make_intake_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "node_path": [],
            "errors": [],
        }

        result = await node_fn(state)

        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Tests: classify_node
# ---------------------------------------------------------------------------


class TestClassifyNode:
    """Tests for the classify node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_build_intent_classification(self, deps):
        """Build intent message is classified as build_intent."""
        node_fn = make_classify_node(deps)
        state: ForgeState = {
            "message": "build user authentication",
            "status": "",
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["intent"] == "build_intent"
        assert "classify" in result["node_path"]

    async def test_status_query_classification(self, deps):
        """Status query message is classified as status_query."""
        node_fn = make_classify_node(deps)
        state: ForgeState = {
            "message": "status",
            "status": "",
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["intent"] == "status_query"

    async def test_interrupt_classification(self, deps):
        """Interrupt message is classified as interrupt when build is active."""
        node_fn = make_classify_node(deps)
        state: ForgeState = {
            "message": "stop",
            "status": "processing",
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["intent"] == "interrupt"

    async def test_natural_language_fallback(self, deps):
        """Unrecognized message falls back to natural_language."""
        node_fn = make_classify_node(deps)
        state: ForgeState = {
            "message": "hello how are you today",
            "status": "",
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["intent"] == "natural_language"


# ---------------------------------------------------------------------------
# Tests: clarify_node
# ---------------------------------------------------------------------------


class TestClarifyNode:
    """Tests for the clarify node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_sets_needs_clarification_flag(self, deps):
        """Clarify node sets needs_clarification when inputs are missing."""
        node_fn = make_clarify_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "build something",
            "node_path": [],
        }

        result = await node_fn(state)

        # With an empty context, missing inputs should trigger clarification
        assert result["needs_clarification"] is True
        assert "clarify" in result["node_path"]


# ---------------------------------------------------------------------------
# Tests: architect_node
# ---------------------------------------------------------------------------


class TestArchitectNode:
    """Tests for the architect node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_produces_spec_and_tasks(self, deps):
        """Architect node produces spec_artifact_uri and tasks list."""
        node_fn = make_architect_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "Build user auth",
            "node_path": [],
        }

        result = await node_fn(state)

        assert "spec_artifact_uri" in result
        assert "tasks" in result
        assert len(result["tasks"]) > 0
        assert "architect" in result["node_path"]

    async def test_emits_spec_ready_and_tasks_ready(self, deps):
        """Architect node emits spec.ready and tasks.ready events."""
        node_fn = make_architect_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "Build user auth",
            "node_path": [],
        }

        await node_fn(state)

        # Should have called publish twice (spec.ready + tasks.ready)
        assert deps.event_bus.publish.call_count == 2


# ---------------------------------------------------------------------------
# Tests: plan_node
# ---------------------------------------------------------------------------


class TestPlanNode:
    """Tests for the plan node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_produces_topological_ordering(self, deps):
        """Plan node produces a valid topological ordering."""
        node_fn = make_plan_node(deps)
        state: ForgeState = {
            "tasks": [
                Task(id="t1", title="Task 1", depends_on=[]),
                Task(id="t2", title="Task 2", depends_on=["t1"]),
                Task(id="t3", title="Task 3", depends_on=["t1"]),
            ],
            "node_path": [],
            "errors": [],
        }

        result = await node_fn(state)

        assert "task_ordering" in result
        assert result["current_task_index"] == 0
        # t1 must come before t2 and t3
        ordering = result["task_ordering"]
        assert ordering.index("t1") < ordering.index("t2")
        assert ordering.index("t1") < ordering.index("t3")
        assert "plan" in result["node_path"]

    async def test_cycle_detection(self, deps):
        """Plan node detects cycles and sets status to plan_failed."""
        node_fn = make_plan_node(deps)
        state: ForgeState = {
            "tasks": [
                Task(id="t1", title="Task 1", depends_on=["t2"]),
                Task(id="t2", title="Task 2", depends_on=["t1"]),
            ],
            "node_path": [],
            "errors": [],
        }

        result = await node_fn(state)

        assert result["status"] == "plan_failed"
        assert len(result["errors"]) > 0
        assert "plan" in result["node_path"]


# ---------------------------------------------------------------------------
# Tests: execute_node
# ---------------------------------------------------------------------------


class TestExecuteNode:
    """Tests for the execute node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_creates_workspace_and_sets_task(self, deps):
        """Execute node creates workspace and sets current_task_id."""
        node_fn = make_execute_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "task_ordering": ["t1", "t2"],
            "current_task_index": 0,
            "tasks": [
                Task(id="t1", title="Task 1"),
                Task(id="t2", title="Task 2"),
            ],
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["current_task_id"] == "t1"
        deps.workspace_manager.create.assert_called_once()
        assert "execute" in result["node_path"]


# ---------------------------------------------------------------------------
# Tests: verify_node
# ---------------------------------------------------------------------------


class TestVerifyNode:
    """Tests for the verify node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_records_verification_results(self, deps):
        """Verify node records results in verification_results."""
        node_fn = make_verify_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "current_task_id": "t1",
            "verification_results": {},
            "node_path": [],
        }

        result = await node_fn(state)

        assert "verification_results" in result
        assert "t1" in result["verification_results"]
        assert result["verification_results"]["t1"]["passed"] is True
        assert "verify" in result["node_path"]

    async def test_no_task_id_still_appends_node_path(self, deps):
        """Verify node with no current task still appends to node_path."""
        node_fn = make_verify_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "node_path": [],
        }

        result = await node_fn(state)

        assert "verify" in result["node_path"]


# ---------------------------------------------------------------------------
# Tests: commit_node
# ---------------------------------------------------------------------------


class TestCommitNode:
    """Tests for the commit node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_appends_sha_and_increments_index(self, deps):
        """Commit node appends SHA and increments current_task_index."""
        node_fn = make_commit_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "commit_shas": [],
            "current_task_index": 0,
            "task_ordering": ["t1", "t2"],
            "current_task_id": "t1",
            "node_path": [],
        }

        result = await node_fn(state)

        assert len(result["commit_shas"]) == 1
        assert result["current_task_index"] == 1
        assert result["all_tasks_done"] is False
        assert "commit" in result["node_path"]

    async def test_sets_all_tasks_done_on_last_task(self, deps):
        """Commit node sets all_tasks_done when last task commits."""
        node_fn = make_commit_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "commit_shas": ["abc123"],
            "current_task_index": 1,
            "task_ordering": ["t1", "t2"],
            "current_task_id": "t2",
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["all_tasks_done"] is True
        assert result["current_task_index"] == 2

    async def test_emits_commit_done_event(self, deps):
        """Commit node emits a commit.done event."""
        node_fn = make_commit_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "commit_shas": [],
            "current_task_index": 0,
            "task_ordering": ["t1"],
            "current_task_id": "t1",
            "node_path": [],
        }

        await node_fn(state)

        deps.event_bus.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: doc_update_node
# ---------------------------------------------------------------------------


class TestDocUpdateNode:
    """Tests for the doc_update node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_populates_doc_updates(self, deps):
        """Doc update node populates doc_updates list."""
        node_fn = make_doc_update_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "commit_shas": ["abc123"],
            "node_path": [],
        }

        result = await node_fn(state)

        assert "doc_updates" in result
        assert isinstance(result["doc_updates"], list)
        assert "doc_update" in result["node_path"]


# ---------------------------------------------------------------------------
# Tests: finalize_node
# ---------------------------------------------------------------------------


class TestFinalizeNode:
    """Tests for the finalize node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_sets_status_completed(self, deps):
        """Finalize node sets status to completed."""
        node_fn = make_finalize_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "commit_shas": ["abc123"],
            "tasks": [],
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["status"] == "completed"
        assert "finalize" in result["node_path"]

    async def test_emits_build_done_event(self, deps):
        """Finalize node emits build.done event."""
        node_fn = make_finalize_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "commit_shas": ["abc"],
            "tasks": [],
            "node_path": [],
        }

        await node_fn(state)

        deps.event_bus.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: status_node
# ---------------------------------------------------------------------------


class TestStatusNode:
    """Tests for the status node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_delegates_to_inspector(self, deps):
        """Status node delegates to deps.inspector.get_status()."""
        node_fn = make_status_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "node_path": [],
        }

        result = await node_fn(state)

        deps.inspector.get_status.assert_called_once_with("sess-1")
        assert "status" in result["node_path"]


# ---------------------------------------------------------------------------
# Tests: interrupt_node
# ---------------------------------------------------------------------------


class TestInterruptNode:
    """Tests for the interrupt node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_pauses_and_sets_interrupted(self, deps):
        """Interrupt node calls pause and sets status to interrupted."""
        node_fn = make_interrupt_node(deps)
        state: ForgeState = {
            "session_id": "sess-1",
            "message": "stop",
            "node_path": [],
        }

        result = await node_fn(state)

        assert result["status"] == "interrupted"
        deps.interrupt_handler.pause.assert_called_once()
        assert "interrupt" in result["node_path"]


# ---------------------------------------------------------------------------
# Tests: policy_node
# ---------------------------------------------------------------------------


class TestPolicyNode:
    """Tests for the policy node function."""

    @pytest.fixture
    def deps(self):
        return make_mock_deps()

    async def test_records_decision(self, deps):
        """Policy node records decision in decisions list."""
        node_fn = make_policy_node(deps)
        state: ForgeState = {
            "current_task_id": "t1",
            "verification_results": {"t1": {"halted_at": "lint", "passed": False}},
            "tasks": [Task(id="t1", title="Task 1", attempts=1)],
            "decisions": [],
            "node_path": [],
        }

        result = await node_fn(state)

        assert len(result["decisions"]) == 1
        assert "retry" in result["decisions"][0]
        deps.policy_engine.decide.assert_called_once()
        assert "policy" in result["node_path"]
