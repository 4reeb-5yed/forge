"""Task Dispatcher — sequential task execution with workspace isolation.

Implements the task execution engine that:
- Accepts a topological task ordering from the planner
- Executes tasks one at a time in dependency order (parallelism=1)
- Requests an isolated workspace before any file-system write
- Confines all task file-system writes to the assigned workspace path
- Never writes to the canonical repository during execution
- Emits task.start and task.done events

Requirements: 6.1, 6.4, 6.5
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from app.runtime.events.models import Event, EventType


class WorkspaceProvider(Protocol):
    """Protocol for the WorkspaceManager dependency.

    The TaskDispatcher uses this to request and release isolated workspaces.
    """

    async def create_workspace(self, session_id: str, task_id: str, base_ref: str) -> str:
        """Create an isolated workspace for a task.

        Returns the workspace path where the task can write files.
        Raises WorkspaceCreationError if workspace cannot be created.
        """
        ...

    async def destroy_workspace(self, workspace_id: str) -> None:
        """Destroy a workspace after task completion or merge."""
        ...


class EventPublisher(Protocol):
    """Protocol for publishing events to the event bus."""

    async def publish(self, event: Event) -> Event:
        """Publish an event to the bus."""
        ...


class TaskStatus(str, Enum):
    """Status of a task within the dispatcher."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DispatchedTask:
    """A task tracked by the dispatcher with its execution state."""

    id: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    workspace_id: str | None = None
    workspace_path: str | None = None
    error: str | None = None


class WorkspaceCreationError(Exception):
    """Raised when workspace creation fails."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Workspace creation failed for task '{task_id}': {reason}")


class TaskExecutionError(Exception):
    """Raised when a task executor reports failure."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Task '{task_id}' execution failed: {reason}")


# Type for the task executor callback
TaskExecutor = Callable[[DispatchedTask, str], Awaitable[None]]


@dataclass
class DispatchResult:
    """Result of dispatching a full task ordering.

    Attributes:
        completed: Task IDs that completed successfully.
        failed: Task IDs that failed.
        skipped: Task IDs that were skipped (due to dependency failure).
    """

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        """True if every task completed without failure or skip."""
        return len(self.failed) == 0 and len(self.skipped) == 0


class TaskDispatcher:
    """Sequential task dispatcher with workspace isolation.

    Executes tasks one at a time in dependency order. Before executing
    a task, requests an isolated workspace from the WorkspaceProvider.
    All file-system writes during task execution are confined to the
    assigned workspace. The canonical repository is never written to
    during task execution.

    Requirements:
        6.1 — Request isolated workspace before any file-system write.
        6.4 — Confine all task writes to assigned workspace.
        6.5 — Execute ready tasks one at a time (parallelism=1).
    """

    def __init__(
        self,
        workspace_provider: WorkspaceProvider,
        event_publisher: EventPublisher,
        canonical_path: str,
        session_id: str,
        base_ref: str = "HEAD",
    ) -> None:
        """Initialize the TaskDispatcher.

        Args:
            workspace_provider: Provider for creating/destroying isolated workspaces.
            event_publisher: Publisher for task lifecycle events.
            canonical_path: Path to the canonical repository (never written to).
            session_id: Session identifier for event correlation.
            base_ref: The base ref for workspace creation.
        """
        self._workspace_provider = workspace_provider
        self._event_publisher = event_publisher
        self._canonical_path = str(Path(canonical_path).resolve())
        self._session_id = session_id
        self._base_ref = base_ref

        self._tasks: dict[str, DispatchedTask] = {}
        self._ordering: list[str] = []
        self._current_task_id: str | None = None
        self._lock = asyncio.Lock()

    @property
    def canonical_path(self) -> str:
        """The canonical repository path (read-only, never written to)."""
        return self._canonical_path

    @property
    def current_task_id(self) -> str | None:
        """The ID of the currently executing task, or None."""
        return self._current_task_id

    @property
    def tasks(self) -> dict[str, DispatchedTask]:
        """All tracked tasks keyed by ID."""
        return dict(self._tasks)

    @property
    def pending_task_ids(self) -> list[str]:
        """Task IDs in pending state."""
        return [
            tid for tid in self._ordering
            if self._tasks[tid].status == TaskStatus.PENDING
        ]

    @property
    def completed_task_ids(self) -> list[str]:
        """Task IDs in completed state."""
        return [
            tid for tid in self._ordering
            if self._tasks[tid].status == TaskStatus.COMPLETED
        ]

    @property
    def failed_task_ids(self) -> list[str]:
        """Task IDs in failed state."""
        return [
            tid for tid in self._ordering
            if self._tasks[tid].status == TaskStatus.FAILED
        ]

    @property
    def skipped_task_ids(self) -> list[str]:
        """Task IDs in skipped state."""
        return [
            tid for tid in self._ordering
            if self._tasks[tid].status == TaskStatus.SKIPPED
        ]

    def load_ordering(self, tasks: list[dict[str, Any]], ordering: list[str]) -> None:
        """Load tasks and their topological ordering into the dispatcher.

        Args:
            tasks: List of task dicts (id, title, depends_on).
            ordering: Topological ordering of task IDs from the planner.
        """
        self._tasks.clear()
        self._ordering = list(ordering)

        task_map = {t["id"]: t for t in tasks}
        for task_id in ordering:
            task_data = task_map[task_id]
            self._tasks[task_id] = DispatchedTask(
                id=task_data["id"],
                title=task_data.get("title", ""),
                depends_on=task_data.get("depends_on", []) or [],
            )

    async def dispatch_all(self, executor: TaskExecutor) -> DispatchResult:
        """Execute all tasks sequentially in dependency order.

        For each task:
        1. Check if dependencies are satisfied (skip if any failed/skipped).
        2. Request an isolated workspace from WorkspaceProvider.
        3. Execute the task within the workspace via the executor callback.
        4. Mark task as completed or failed.
        5. Request workspace destruction.

        Tasks are executed one at a time (parallelism=1). The canonical
        repository is never written to during execution.

        Args:
            executor: Async callback that performs the actual task work.
                Receives the DispatchedTask and the workspace path.

        Returns:
            DispatchResult summarizing completed, failed, and skipped tasks.
        """
        async with self._lock:
            result = DispatchResult()

            for task_id in self._ordering:
                task = self._tasks[task_id]

                # Check if dependencies are satisfied
                if not self._dependencies_satisfied(task_id):
                    task.status = TaskStatus.SKIPPED
                    task.error = "Dependency failed or skipped"
                    result.skipped.append(task_id)
                    continue

                # Execute the task
                success = await self._execute_task(task, executor)

                if success:
                    result.completed.append(task_id)
                else:
                    result.failed.append(task_id)
                    # Note: tasks depending on this one will be skipped

            self._current_task_id = None
            return result

    async def _execute_task(self, task: DispatchedTask, executor: TaskExecutor) -> bool:
        """Execute a single task with workspace isolation.

        Returns True on success, False on failure.
        """
        self._current_task_id = task.id
        task.status = TaskStatus.RUNNING

        # Emit task.start event
        await self._emit_task_start(task)

        # Step 1: Request isolated workspace (Req 6.1)
        try:
            workspace_path = await self._workspace_provider.create_workspace(
                session_id=self._session_id,
                task_id=task.id,
                base_ref=self._base_ref,
            )
        except Exception as e:
            # Workspace creation failed — do not begin execution (Req 6.3)
            task.status = TaskStatus.FAILED
            task.error = f"Workspace creation failed: {e}"
            await self._emit_task_done(task, success=False)
            return False

        task.workspace_id = f"ws-{task.id}"
        task.workspace_path = workspace_path

        # Step 2: Execute task confined to workspace (Req 6.4)
        try:
            await executor(task, workspace_path)
            task.status = TaskStatus.COMPLETED
            await self._emit_task_done(task, success=True)
            success = True
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            await self._emit_task_done(task, success=False)
            success = False

        # Step 3: Request workspace destruction
        try:
            await self._workspace_provider.destroy_workspace(
                task.workspace_id or f"ws-{task.id}"
            )
        except Exception:
            # Workspace destruction failure is non-fatal
            pass

        return success

    def _dependencies_satisfied(self, task_id: str) -> bool:
        """Check if all dependencies of a task have completed successfully."""
        task = self._tasks[task_id]
        for dep_id in task.depends_on:
            dep_task = self._tasks.get(dep_id)
            if dep_task is None:
                return False
            if dep_task.status != TaskStatus.COMPLETED:
                return False
        return True

    def is_write_allowed(self, path: str, task_id: str | None = None) -> bool:
        """Check if a write to the given path is allowed.

        Writes are only allowed to the task's assigned workspace path.
        Writes to the canonical repository are NEVER allowed during execution.

        Args:
            path: The file-system path being written to.
            task_id: The task attempting the write. Defaults to current task.

        Returns:
            True if the write is confined to the assigned workspace.
        """
        resolved = str(Path(path).resolve())

        # Never allow writes to canonical repository (Req 6.4)
        if resolved.startswith(self._canonical_path):
            return False

        # Check task workspace confinement
        tid = task_id or self._current_task_id
        if tid is None:
            return False

        task = self._tasks.get(tid)
        if task is None or task.workspace_path is None:
            return False

        workspace_resolved = str(Path(task.workspace_path).resolve())
        return resolved.startswith(workspace_resolved)

    async def _emit_task_start(self, task: DispatchedTask) -> None:
        """Emit a task.start event."""
        event = Event.create(
            type=EventType.TASK_START,
            session_id=self._session_id,
            source="task_dispatcher",
            payload={
                "task_id": task.id,
                "title": task.title,
                "workspace_id": task.workspace_id,
            },
            correlation_id=self._session_id,
            event_id=f"dispatch-task-start-{task.id}",
        )
        await self._event_publisher.publish(event)

    async def _emit_task_done(self, task: DispatchedTask, success: bool) -> None:
        """Emit a task.done or task.fail event."""
        if success:
            event = Event.create(
                type=EventType.TASK_DONE,
                session_id=self._session_id,
                source="task_dispatcher",
                payload={
                    "task_id": task.id,
                    "title": task.title,
                    "workspace_id": task.workspace_id,
                    "status": "completed",
                },
                correlation_id=self._session_id,
                event_id=f"dispatch-task-done-{task.id}",
            )
        else:
            event = Event.create(
                type=EventType.TASK_FAIL,
                session_id=self._session_id,
                source="task_dispatcher",
                payload={
                    "task_id": task.id,
                    "title": task.title,
                    "workspace_id": task.workspace_id,
                    "status": "failed",
                    "error": task.error or "Unknown error",
                },
                correlation_id=self._session_id,
                event_id=f"dispatch-task-fail-{task.id}",
            )
        await self._event_publisher.publish(event)
