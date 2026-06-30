"""Unit tests for the TaskDispatcher with workspace isolation.

Tests sequential execution, workspace isolation, dependency handling,
and event emission.

Requirements: 6.1, 6.4, 6.5
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.runtime.dispatcher import (
    DispatchedTask,
    DispatchResult,
    TaskDispatcher,
    TaskExecutor,
    TaskStatus,
    WorkspaceCreationError,
)
from app.runtime.events.models import Event, EventType


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeWorkspaceProvider:
    """Fake WorkspaceProvider that tracks create/destroy calls."""

    def __init__(self, workspace_base: str = "/tmp/workspaces") -> None:
        self.workspace_base = workspace_base
        self.created: list[dict[str, str]] = []
        self.destroyed: list[str] = []
        self.fail_on_create: bool = False
        self.fail_reason: str = "disk full"

    async def create_workspace(self, session_id: str, task_id: str, base_ref: str) -> str:
        if self.fail_on_create:
            raise WorkspaceCreationError(task_id, self.fail_reason)
        workspace_path = f"{self.workspace_base}/{session_id}/{task_id}"
        self.created.append({
            "session_id": session_id,
            "task_id": task_id,
            "base_ref": base_ref,
            "path": workspace_path,
        })
        return workspace_path

    async def destroy_workspace(self, workspace_id: str) -> None:
        self.destroyed.append(workspace_id)


class FakeEventPublisher:
    """Fake EventPublisher that collects emitted events."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_provider() -> FakeWorkspaceProvider:
    return FakeWorkspaceProvider()


@pytest.fixture
def event_publisher() -> FakeEventPublisher:
    return FakeEventPublisher()


@pytest.fixture
def canonical_path(tmp_path: Path) -> str:
    repo_path = tmp_path / "canonical_repo"
    repo_path.mkdir()
    return str(repo_path)


@pytest.fixture
def dispatcher(
    workspace_provider: FakeWorkspaceProvider,
    event_publisher: FakeEventPublisher,
    canonical_path: str,
) -> TaskDispatcher:
    return TaskDispatcher(
        workspace_provider=workspace_provider,
        event_publisher=event_publisher,
        canonical_path=canonical_path,
        session_id="session-1",
        base_ref="main",
    )


def make_tasks(specs: list[tuple[str, list[str]]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Helper to create task list and ordering from (id, depends_on) tuples.

    Returns tasks in dependency order (simple topological sort).
    """
    tasks = [
        {"id": tid, "title": f"Task {tid}", "depends_on": deps}
        for tid, deps in specs
    ]
    # Simple topological ordering based on input order
    ordering = [tid for tid, _ in specs]
    return tasks, ordering


# ---------------------------------------------------------------------------
# Tests: Sequential execution (Req 6.5)
# ---------------------------------------------------------------------------


class TestSequentialExecution:
    """Tasks are executed one at a time in dependency order."""

    async def test_single_task_executes(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """A single task executes and completes."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        execution_log: list[str] = []

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            execution_log.append(task.id)

        result = await dispatcher.dispatch_all(executor)

        assert result.all_succeeded
        assert result.completed == ["t1"]
        assert execution_log == ["t1"]

    async def test_tasks_execute_in_order(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Tasks execute in the topological order provided."""
        tasks, ordering = make_tasks([("t1", []), ("t2", ["t1"]), ("t3", ["t2"])])
        dispatcher.load_ordering(tasks, ordering)

        execution_order: list[str] = []

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            execution_order.append(task.id)

        result = await dispatcher.dispatch_all(executor)

        assert result.all_succeeded
        assert execution_order == ["t1", "t2", "t3"]

    async def test_parallelism_is_one(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Only one task runs at a time (parallelism=1)."""
        tasks, ordering = make_tasks([("t1", []), ("t2", []), ("t3", [])])
        dispatcher.load_ordering(tasks, ordering)

        concurrent_count = 0
        max_concurrent = 0

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.01)  # Simulate some work
            concurrent_count -= 1

        await dispatcher.dispatch_all(executor)

        assert max_concurrent == 1

    async def test_empty_ordering(self, dispatcher: TaskDispatcher) -> None:
        """An empty ordering produces an empty result."""
        dispatcher.load_ordering([], [])

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            raise AssertionError("Should not be called")

        result = await dispatcher.dispatch_all(executor)

        assert result.all_succeeded
        assert result.completed == []
        assert result.failed == []
        assert result.skipped == []


# ---------------------------------------------------------------------------
# Tests: Workspace isolation (Req 6.1, 6.4)
# ---------------------------------------------------------------------------


class TestWorkspaceIsolation:
    """Tasks execute in isolated workspaces."""

    async def test_workspace_requested_before_execution(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """Workspace is created before the executor runs (Req 6.1)."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        workspace_created_before_exec = False

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            nonlocal workspace_created_before_exec
            workspace_created_before_exec = len(workspace_provider.created) > 0

        await dispatcher.dispatch_all(executor)

        assert workspace_created_before_exec
        assert workspace_provider.created[0]["task_id"] == "t1"
        assert workspace_provider.created[0]["session_id"] == "session-1"
        assert workspace_provider.created[0]["base_ref"] == "main"

    async def test_executor_receives_workspace_path(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """The executor receives the workspace path for file writes."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        received_path: str | None = None

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            nonlocal received_path
            received_path = workspace_path

        await dispatcher.dispatch_all(executor)

        expected = f"{workspace_provider.workspace_base}/session-1/t1"
        assert received_path == expected

    async def test_each_task_gets_own_workspace(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """Each task gets its own isolated workspace."""
        tasks, ordering = make_tasks([("t1", []), ("t2", []), ("t3", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        assert len(workspace_provider.created) == 3
        paths = [c["path"] for c in workspace_provider.created]
        assert len(set(paths)) == 3  # All unique

    async def test_workspace_destroyed_after_completion(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """Workspace is destroyed after task completes."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        assert len(workspace_provider.destroyed) == 1

    async def test_workspace_destroyed_after_failure(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """Workspace is destroyed even if task fails."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            raise RuntimeError("task failed")

        await dispatcher.dispatch_all(executor)

        assert len(workspace_provider.destroyed) == 1


# ---------------------------------------------------------------------------
# Tests: Never write to canonical repository (Req 6.4)
# ---------------------------------------------------------------------------


class TestCanonicalProtection:
    """Canonical repository is never written to during task execution."""

    async def test_write_to_canonical_not_allowed(
        self, dispatcher: TaskDispatcher, canonical_path: str
    ) -> None:
        """is_write_allowed rejects writes to canonical path."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            # Verify canonical path writes are blocked
            assert not dispatcher.is_write_allowed(canonical_path)
            assert not dispatcher.is_write_allowed(f"{canonical_path}/src/main.py")

        await dispatcher.dispatch_all(executor)

    async def test_write_to_workspace_is_allowed(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """is_write_allowed permits writes to the assigned workspace."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        write_allowed = False

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            nonlocal write_allowed
            write_allowed = dispatcher.is_write_allowed(f"{workspace_path}/src/file.py")

        await dispatcher.dispatch_all(executor)

        assert write_allowed

    async def test_write_to_other_task_workspace_not_allowed(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """is_write_allowed rejects writes to another task's workspace."""
        tasks, ordering = make_tasks([("t1", []), ("t2", [])])
        dispatcher.load_ordering(tasks, ordering)

        t2_workspace = f"{workspace_provider.workspace_base}/session-1/t2"

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            if task.id == "t1":
                # t1 should not be able to write to t2's workspace
                assert not dispatcher.is_write_allowed(t2_workspace)

        await dispatcher.dispatch_all(executor)

    def test_write_not_allowed_with_no_current_task(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """is_write_allowed returns False when no task is executing."""
        assert not dispatcher.is_write_allowed("/some/random/path")


# ---------------------------------------------------------------------------
# Tests: Dependency handling
# ---------------------------------------------------------------------------


class TestDependencyHandling:
    """Tasks with failed dependencies are skipped."""

    async def test_dependent_task_skipped_on_failure(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """If a task fails, tasks depending on it are skipped."""
        tasks, ordering = make_tasks([("t1", []), ("t2", ["t1"]), ("t3", ["t2"])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            if task.id == "t1":
                raise RuntimeError("t1 failed")

        result = await dispatcher.dispatch_all(executor)

        assert result.failed == ["t1"]
        assert result.skipped == ["t2", "t3"]
        assert result.completed == []

    async def test_independent_tasks_continue_after_failure(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Tasks not depending on a failed task still execute."""
        tasks = [
            {"id": "t1", "title": "Task t1", "depends_on": []},
            {"id": "t2", "title": "Task t2", "depends_on": []},
            {"id": "t3", "title": "Task t3", "depends_on": ["t1"]},
        ]
        ordering = ["t1", "t2", "t3"]
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            if task.id == "t1":
                raise RuntimeError("t1 failed")

        result = await dispatcher.dispatch_all(executor)

        assert "t1" in result.failed
        assert "t2" in result.completed
        assert "t3" in result.skipped

    async def test_workspace_creation_failure_skips_dependents(
        self,
        dispatcher: TaskDispatcher,
        workspace_provider: FakeWorkspaceProvider,
    ) -> None:
        """If workspace creation fails, the task fails and dependents are skipped."""
        tasks, ordering = make_tasks([("t1", []), ("t2", ["t1"])])
        dispatcher.load_ordering(tasks, ordering)
        workspace_provider.fail_on_create = True

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass  # Should never be reached

        result = await dispatcher.dispatch_all(executor)

        assert "t1" in result.failed
        assert "t2" in result.skipped

    async def test_all_tasks_complete_when_no_failures(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """All tasks complete when there are no failures."""
        tasks, ordering = make_tasks([
            ("t1", []),
            ("t2", ["t1"]),
            ("t3", ["t1"]),
            ("t4", ["t2", "t3"]),
        ])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        result = await dispatcher.dispatch_all(executor)

        assert result.all_succeeded
        assert set(result.completed) == {"t1", "t2", "t3", "t4"}


# ---------------------------------------------------------------------------
# Tests: Event emission
# ---------------------------------------------------------------------------


class TestEventEmission:
    """TaskDispatcher emits task.start and task.done/task.fail events."""

    async def test_task_start_event_emitted(
        self, dispatcher: TaskDispatcher, event_publisher: FakeEventPublisher
    ) -> None:
        """A task.start event is emitted before task execution."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        start_events = event_publisher.events_of_type(EventType.TASK_START)
        assert len(start_events) == 1
        assert start_events[0].payload["task_id"] == "t1"
        assert start_events[0].session_id == "session-1"
        assert start_events[0].source == "task_dispatcher"

    async def test_task_done_event_on_success(
        self, dispatcher: TaskDispatcher, event_publisher: FakeEventPublisher
    ) -> None:
        """A task.done event is emitted on successful completion."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        done_events = event_publisher.events_of_type(EventType.TASK_DONE)
        assert len(done_events) == 1
        assert done_events[0].payload["task_id"] == "t1"
        assert done_events[0].payload["status"] == "completed"

    async def test_task_fail_event_on_failure(
        self, dispatcher: TaskDispatcher, event_publisher: FakeEventPublisher
    ) -> None:
        """A task.fail event is emitted on task failure."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            raise RuntimeError("something broke")

        await dispatcher.dispatch_all(executor)

        fail_events = event_publisher.events_of_type(EventType.TASK_FAIL)
        assert len(fail_events) == 1
        assert fail_events[0].payload["task_id"] == "t1"
        assert fail_events[0].payload["status"] == "failed"
        assert "something broke" in fail_events[0].payload["error"]

    async def test_task_fail_event_on_workspace_creation_failure(
        self,
        dispatcher: TaskDispatcher,
        event_publisher: FakeEventPublisher,
        workspace_provider: FakeWorkspaceProvider,
    ) -> None:
        """A task.fail event is emitted when workspace creation fails."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)
        workspace_provider.fail_on_create = True

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        # Should have task.start followed by task.fail
        start_events = event_publisher.events_of_type(EventType.TASK_START)
        fail_events = event_publisher.events_of_type(EventType.TASK_FAIL)
        assert len(start_events) == 1
        assert len(fail_events) == 1

    async def test_event_ordering_start_before_done(
        self, dispatcher: TaskDispatcher, event_publisher: FakeEventPublisher
    ) -> None:
        """task.start is emitted before task.done for each task."""
        tasks, ordering = make_tasks([("t1", []), ("t2", ["t1"])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        event_types = [e.type for e in event_publisher.events]
        # Pattern: start, done, start, done
        assert event_types == [
            EventType.TASK_START,
            EventType.TASK_DONE,
            EventType.TASK_START,
            EventType.TASK_DONE,
        ]


# ---------------------------------------------------------------------------
# Tests: Task state tracking
# ---------------------------------------------------------------------------


class TestTaskStateTracking:
    """TaskDispatcher tracks task states correctly."""

    async def test_current_task_id_during_execution(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """current_task_id is set during execution."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        seen_current: str | None = None

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            nonlocal seen_current
            seen_current = dispatcher.current_task_id

        await dispatcher.dispatch_all(executor)

        assert seen_current == "t1"
        # After dispatch, current_task_id should be None
        assert dispatcher.current_task_id is None

    async def test_task_statuses_after_dispatch(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Task statuses are correctly updated after dispatch."""
        tasks, ordering = make_tasks([("t1", []), ("t2", ["t1"]), ("t3", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            if task.id == "t1":
                raise RuntimeError("failed")

        await dispatcher.dispatch_all(executor)

        task_states = dispatcher.tasks
        assert task_states["t1"].status == TaskStatus.FAILED
        assert task_states["t2"].status == TaskStatus.SKIPPED
        assert task_states["t3"].status == TaskStatus.COMPLETED

    async def test_pending_completed_failed_skipped_properties(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Property accessors correctly reflect task state."""
        tasks, ordering = make_tasks([("t1", []), ("t2", ["t1"]), ("t3", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            if task.id == "t1":
                raise RuntimeError("failed")

        await dispatcher.dispatch_all(executor)

        assert dispatcher.completed_task_ids == ["t3"]
        assert dispatcher.failed_task_ids == ["t1"]
        assert dispatcher.skipped_task_ids == ["t2"]
        assert dispatcher.pending_task_ids == []


# ---------------------------------------------------------------------------
# Tests: DispatchResult
# ---------------------------------------------------------------------------


class TestDispatchResult:
    """Tests for the DispatchResult dataclass."""

    def test_all_succeeded_true_when_no_failures(self) -> None:
        result = DispatchResult(completed=["t1", "t2"])
        assert result.all_succeeded

    def test_all_succeeded_false_with_failures(self) -> None:
        result = DispatchResult(completed=["t1"], failed=["t2"])
        assert not result.all_succeeded

    def test_all_succeeded_false_with_skips(self) -> None:
        result = DispatchResult(completed=["t1"], skipped=["t2"])
        assert not result.all_succeeded

    def test_empty_result_is_all_succeeded(self) -> None:
        result = DispatchResult()
        assert result.all_succeeded


# ---------------------------------------------------------------------------
# Tests: Additional coverage for requirements 6.1–6.7
# ---------------------------------------------------------------------------


class TestWorkspaceDestroyExactlyOnce:
    """Requirement 6.6: Workspace is destroyed exactly once per task."""

    async def test_workspace_destroyed_exactly_once_on_success(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """Each task's workspace is destroyed exactly once on success."""
        tasks, ordering = make_tasks([("t1", []), ("t2", []), ("t3", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        # 3 tasks → exactly 3 destroy calls
        assert len(workspace_provider.destroyed) == 3

    async def test_workspace_destroyed_exactly_once_on_failure(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """A failed task's workspace is still destroyed exactly once."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            raise RuntimeError("boom")

        await dispatcher.dispatch_all(executor)

        assert len(workspace_provider.destroyed) == 1


class TestWriteConfinementEdgeCases:
    """Requirement 6.4: Additional write confinement edge cases."""

    async def test_write_to_arbitrary_path_not_allowed(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Writes to a path outside both canonical and workspace are rejected."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            # Arbitrary path that isn't canonical or workspace
            assert not dispatcher.is_write_allowed("/some/random/unrelated/path")
            assert not dispatcher.is_write_allowed("C:\\Windows\\System32\\evil.dll")

        await dispatcher.dispatch_all(executor)

    async def test_write_to_subdirectory_of_workspace_allowed(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """Writes to nested subdirectories within workspace are allowed."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            assert dispatcher.is_write_allowed(f"{workspace_path}/deep/nested/dir/file.py")

        await dispatcher.dispatch_all(executor)


class TestExecutorNotCalledOnWorkspaceFailure:
    """Requirement 6.3: Executor SHALL NOT be called if workspace creation fails."""

    async def test_executor_never_invoked_on_workspace_failure(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """The executor callback is never called for a task whose workspace fails."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)
        workspace_provider.fail_on_create = True

        executor_called = False

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            nonlocal executor_called
            executor_called = True

        await dispatcher.dispatch_all(executor)

        assert not executor_called


class TestDiamondDependency:
    """Requirement 6.5: Diamond dependency pattern executes correctly."""

    async def test_diamond_dependency_all_execute(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Diamond: t1 → t2, t1 → t3, t2+t3 → t4. All execute in valid order."""
        tasks = [
            {"id": "t1", "title": "root", "depends_on": []},
            {"id": "t2", "title": "left", "depends_on": ["t1"]},
            {"id": "t3", "title": "right", "depends_on": ["t1"]},
            {"id": "t4", "title": "join", "depends_on": ["t2", "t3"]},
        ]
        ordering = ["t1", "t2", "t3", "t4"]
        dispatcher.load_ordering(tasks, ordering)

        execution_order: list[str] = []

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            execution_order.append(task.id)

        result = await dispatcher.dispatch_all(executor)

        assert result.all_succeeded
        assert set(result.completed) == {"t1", "t2", "t3", "t4"}
        # t1 must be before t2, t3; t2 and t3 must be before t4
        assert execution_order.index("t1") < execution_order.index("t2")
        assert execution_order.index("t1") < execution_order.index("t3")
        assert execution_order.index("t2") < execution_order.index("t4")
        assert execution_order.index("t3") < execution_order.index("t4")

    async def test_diamond_with_one_branch_failing(
        self, dispatcher: TaskDispatcher
    ) -> None:
        """Diamond: if one branch fails, the join task is skipped."""
        tasks = [
            {"id": "t1", "title": "root", "depends_on": []},
            {"id": "t2", "title": "left", "depends_on": ["t1"]},
            {"id": "t3", "title": "right", "depends_on": ["t1"]},
            {"id": "t4", "title": "join", "depends_on": ["t2", "t3"]},
        ]
        ordering = ["t1", "t2", "t3", "t4"]
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            if task.id == "t2":
                raise RuntimeError("left branch failed")

        result = await dispatcher.dispatch_all(executor)

        assert "t1" in result.completed
        assert "t2" in result.failed
        assert "t3" in result.completed
        assert "t4" in result.skipped  # depends on failed t2


class TestWorkspaceIdPassedToDestroy:
    """Requirement 6.6: Correct workspace ID is passed to destroy."""

    async def test_destroy_called_with_correct_workspace_id(
        self, dispatcher: TaskDispatcher, workspace_provider: FakeWorkspaceProvider
    ) -> None:
        """The workspace_id passed to destroy corresponds to the created workspace."""
        tasks, ordering = make_tasks([("t1", [])])
        dispatcher.load_ordering(tasks, ordering)

        async def executor(task: DispatchedTask, workspace_path: str) -> None:
            pass

        await dispatcher.dispatch_all(executor)

        # The workspace_id format is "ws-{task_id}" per the dispatcher impl
        assert workspace_provider.destroyed == ["ws-t1"]
