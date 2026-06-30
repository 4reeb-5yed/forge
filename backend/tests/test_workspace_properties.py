"""Property-based tests for workspace isolation using Hypothesis.

**Validates: Requirements 6.4, 6.5**

Property 9: Workspace isolation — no worker writes to the canonical repository;
    all task work happens in an isolated workspace and reaches canonical repo
    only via commit/merge.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st, assume

from app.runtime.dispatcher import TaskDispatcher
from app.runtime.events.bus import EventBus
from app.runtime.events.models import Event, EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeWorkspaceProvider:
    """Fake workspace provider that creates real temp directories for isolation testing."""

    def __init__(self, base_dir: str) -> None:
        self._base_dir = base_dir
        self._workspaces: dict[str, str] = {}

    async def create_workspace(self, session_id: str, task_id: str, base_ref: str) -> str:
        workspace_path = tempfile.mkdtemp(
            prefix=f"ws_{task_id[:8]}_", dir=self._base_dir
        )
        workspace_id = f"ws-{task_id}"
        self._workspaces[workspace_id] = workspace_path
        return workspace_path

    async def destroy_workspace(self, workspace_id: str) -> None:
        self._workspaces.pop(workspace_id, None)


class FakeEventPublisher:
    """Fake event publisher that records published events."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> Event:
        self.events.append(event)
        return event


def make_dispatcher(
    canonical_path: str,
    workspace_provider: FakeWorkspaceProvider,
) -> TaskDispatcher:
    """Create a TaskDispatcher with the given canonical path and workspace provider."""
    publisher = FakeEventPublisher()
    return TaskDispatcher(
        workspace_provider=workspace_provider,
        event_publisher=publisher,
        canonical_path=canonical_path,
        session_id=f"session-{uuid.uuid4().hex[:8]}",
        base_ref="HEAD",
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy for generating arbitrary path segments (safe for filesystem paths)
# Exclude null bytes, path separators, colons (Windows drive letters), and
# other characters that are invalid or problematic in file paths on Windows/POSIX.
path_segment = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        blacklist_characters="\x00/\\:<>|\"?*",
    ),
    min_size=1,
    max_size=20,
).filter(lambda s: s.strip() != "" and s not in (".", ".."))

# Strategy for generating relative path suffixes (1-5 segments joined by os.sep)
relative_path = st.lists(path_segment, min_size=1, max_size=5).map(
    lambda parts: os.sep.join(parts)
)

# Strategy for task IDs
task_id_st = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
    min_size=1,
    max_size=20,
)


# ---------------------------------------------------------------------------
# Property 9: Workspace isolation
# ---------------------------------------------------------------------------


class TestWorkspaceIsolationProperty:
    """Property 9: Workspace isolation — no worker writes to the canonical
    repository; all task work happens in an isolated workspace and reaches
    canonical repo only via commit/merge.

    **Validates: Requirements 6.4, 6.5**
    """

    @given(sub_path=relative_path)
    @settings(max_examples=200, deadline=5000)
    def test_writes_to_canonical_repo_always_rejected(self, sub_path: str) -> None:
        """For any file path within the canonical repository, is_write_allowed()
        must return False, regardless of path structure or depth.

        This guarantees that no task can write directly to the canonical repo.
        """
        with tempfile.TemporaryDirectory() as canonical_dir:
            with tempfile.TemporaryDirectory() as ws_base_dir:
                workspace_provider = FakeWorkspaceProvider(ws_base_dir)
                dispatcher = make_dispatcher(canonical_dir, workspace_provider)

                # Set up a task with a workspace
                task_id = "task-isolation-test"
                workspace_path = tempfile.mkdtemp(
                    prefix="ws_test_", dir=ws_base_dir
                )
                dispatcher.load_ordering(
                    tasks=[{"id": task_id, "title": "Test task", "depends_on": []}],
                    ordering=[task_id],
                )
                # Manually set up task workspace state for testing
                dispatcher._tasks[task_id].workspace_path = workspace_path
                dispatcher._tasks[task_id].status = (
                    __import__("app.runtime.dispatcher", fromlist=["TaskStatus"])
                    .TaskStatus.RUNNING
                )
                dispatcher._current_task_id = task_id

                # Build path inside canonical repo
                target_path = os.path.join(canonical_dir, sub_path)

                # Must ALWAYS be rejected
                assert dispatcher.is_write_allowed(target_path, task_id) is False, (
                    f"Write to canonical repo path should be rejected: {target_path}"
                )

    @given(sub_path=relative_path)
    @settings(max_examples=200, deadline=5000)
    def test_writes_to_own_workspace_always_allowed(self, sub_path: str) -> None:
        """For any file path within the task's own assigned workspace,
        is_write_allowed() must return True.

        This guarantees that task work within its isolated workspace is permitted.
        """
        with tempfile.TemporaryDirectory() as canonical_dir:
            with tempfile.TemporaryDirectory() as ws_base_dir:
                workspace_provider = FakeWorkspaceProvider(ws_base_dir)
                dispatcher = make_dispatcher(canonical_dir, workspace_provider)

                # Create workspace directory
                task_id = "task-workspace-test"
                workspace_path = tempfile.mkdtemp(
                    prefix="ws_test_", dir=ws_base_dir
                )

                dispatcher.load_ordering(
                    tasks=[{"id": task_id, "title": "Test task", "depends_on": []}],
                    ordering=[task_id],
                )
                from app.runtime.dispatcher import TaskStatus

                dispatcher._tasks[task_id].workspace_path = workspace_path
                dispatcher._tasks[task_id].status = TaskStatus.RUNNING
                dispatcher._current_task_id = task_id

                # Build path inside own workspace
                target_path = os.path.join(workspace_path, sub_path)

                # Must ALWAYS be allowed
                assert dispatcher.is_write_allowed(target_path, task_id) is True, (
                    f"Write to own workspace should be allowed: {target_path}"
                )

    @given(
        task_a_sub=relative_path,
        task_b_sub=relative_path,
    )
    @settings(max_examples=150, deadline=5000)
    def test_writes_to_other_tasks_workspace_always_rejected(
        self,
        task_a_sub: str,
        task_b_sub: str,
    ) -> None:
        """For two distinct tasks, each task can only write to its OWN workspace.
        Writing to another task's workspace must be rejected.

        This guarantees cross-task isolation within the same session.
        """
        with tempfile.TemporaryDirectory() as canonical_dir:
            with tempfile.TemporaryDirectory() as ws_base_dir:
                workspace_provider = FakeWorkspaceProvider(ws_base_dir)
                dispatcher = make_dispatcher(canonical_dir, workspace_provider)

                # Create two tasks with separate workspaces
                task_a_id = "task-a"
                task_b_id = "task-b"
                workspace_a = tempfile.mkdtemp(prefix="ws_a_", dir=ws_base_dir)
                workspace_b = tempfile.mkdtemp(prefix="ws_b_", dir=ws_base_dir)

                dispatcher.load_ordering(
                    tasks=[
                        {"id": task_a_id, "title": "Task A", "depends_on": []},
                        {"id": task_b_id, "title": "Task B", "depends_on": []},
                    ],
                    ordering=[task_a_id, task_b_id],
                )
                from app.runtime.dispatcher import TaskStatus

                dispatcher._tasks[task_a_id].workspace_path = workspace_a
                dispatcher._tasks[task_a_id].status = TaskStatus.RUNNING
                dispatcher._tasks[task_b_id].workspace_path = workspace_b
                dispatcher._tasks[task_b_id].status = TaskStatus.RUNNING
                dispatcher._current_task_id = task_a_id

                # Task A trying to write to Task B's workspace — must be rejected
                target_in_b = os.path.join(workspace_b, task_a_sub)
                assert dispatcher.is_write_allowed(target_in_b, task_a_id) is False, (
                    f"Task A should NOT be able to write to Task B's workspace: {target_in_b}"
                )

                # Task B trying to write to Task A's workspace — must be rejected
                target_in_a = os.path.join(workspace_a, task_b_sub)
                assert dispatcher.is_write_allowed(target_in_a, task_b_id) is False, (
                    f"Task B should NOT be able to write to Task A's workspace: {target_in_a}"
                )

                # But each task CAN write to its own workspace
                own_a = os.path.join(workspace_a, task_a_sub)
                assert dispatcher.is_write_allowed(own_a, task_a_id) is True, (
                    f"Task A should be able to write to its own workspace: {own_a}"
                )

                own_b = os.path.join(workspace_b, task_b_sub)
                assert dispatcher.is_write_allowed(own_b, task_b_id) is True, (
                    f"Task B should be able to write to its own workspace: {own_b}"
                )

    @given(sub_path=relative_path)
    @settings(max_examples=100, deadline=5000)
    def test_writes_outside_any_workspace_always_rejected(self, sub_path: str) -> None:
        """For any path that is neither in the canonical repo nor in the task's
        assigned workspace, is_write_allowed() must return False.

        This ensures that tasks cannot write to arbitrary filesystem locations.
        """
        with tempfile.TemporaryDirectory() as canonical_dir:
            with tempfile.TemporaryDirectory() as ws_base_dir:
                with tempfile.TemporaryDirectory() as unrelated_dir:
                    workspace_provider = FakeWorkspaceProvider(ws_base_dir)
                    dispatcher = make_dispatcher(canonical_dir, workspace_provider)

                    task_id = "task-boundary-test"
                    workspace_path = tempfile.mkdtemp(
                        prefix="ws_test_", dir=ws_base_dir
                    )

                    dispatcher.load_ordering(
                        tasks=[{"id": task_id, "title": "Test task", "depends_on": []}],
                        ordering=[task_id],
                    )
                    from app.runtime.dispatcher import TaskStatus

                    dispatcher._tasks[task_id].workspace_path = workspace_path
                    dispatcher._tasks[task_id].status = TaskStatus.RUNNING
                    dispatcher._current_task_id = task_id

                    # Path in an unrelated directory — must be rejected
                    target_path = os.path.join(unrelated_dir, sub_path)
                    assert dispatcher.is_write_allowed(target_path, task_id) is False, (
                        f"Write to unrelated path should be rejected: {target_path}"
                    )

    @given(
        num_tasks=st.integers(min_value=2, max_value=6),
        data=st.data(),
    )
    @settings(max_examples=80, deadline=5000)
    def test_multi_task_isolation_all_combinations(
        self,
        num_tasks: int,
        data: st.DataObject,
    ) -> None:
        """For N tasks with N distinct workspaces, verify that each task can only
        write to its own workspace and never to any other task's workspace or
        the canonical repo.

        This stress-tests isolation across multiple concurrent tasks.
        """
        with tempfile.TemporaryDirectory() as canonical_dir:
            with tempfile.TemporaryDirectory() as ws_base_dir:
                workspace_provider = FakeWorkspaceProvider(ws_base_dir)
                dispatcher = make_dispatcher(canonical_dir, workspace_provider)

                # Create N tasks with distinct workspaces
                task_ids = [f"task-{i}" for i in range(num_tasks)]
                workspaces: dict[str, str] = {}
                for tid in task_ids:
                    workspaces[tid] = tempfile.mkdtemp(
                        prefix=f"ws_{tid}_", dir=ws_base_dir
                    )

                dispatcher.load_ordering(
                    tasks=[
                        {"id": tid, "title": f"Task {tid}", "depends_on": []}
                        for tid in task_ids
                    ],
                    ordering=task_ids,
                )
                from app.runtime.dispatcher import TaskStatus

                for tid in task_ids:
                    dispatcher._tasks[tid].workspace_path = workspaces[tid]
                    dispatcher._tasks[tid].status = TaskStatus.RUNNING

                dispatcher._current_task_id = task_ids[0]

                # For each task, generate a random sub-path
                for tid in task_ids:
                    sub = data.draw(relative_path, label=f"sub_{tid}")

                    # Can write to own workspace
                    own_path = os.path.join(workspaces[tid], sub)
                    assert dispatcher.is_write_allowed(own_path, tid) is True, (
                        f"Task {tid} should write to own workspace"
                    )

                    # Cannot write to canonical repo
                    canon_path = os.path.join(canonical_dir, sub)
                    assert dispatcher.is_write_allowed(canon_path, tid) is False, (
                        f"Task {tid} should NOT write to canonical repo"
                    )

                    # Cannot write to any other task's workspace
                    for other_tid in task_ids:
                        if other_tid == tid:
                            continue
                        other_path = os.path.join(workspaces[other_tid], sub)
                        assert dispatcher.is_write_allowed(other_path, tid) is False, (
                            f"Task {tid} should NOT write to {other_tid}'s workspace"
                        )
