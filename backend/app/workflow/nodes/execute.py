"""Execute node — dispatches current task in an isolated workspace.

Creates a workspace, invokes the coding tool (Aider/SandboxedAider),
and sets state for the verification and commit stages.

Requirements: 8.1, 8.2, 8.3
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState, Task
from app.workflow.deps import RuntimeDeps

logger = logging.getLogger(__name__)

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_execute_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the execute node function.

    The execute node:
    1. Gets the current task from task_ordering
    2. Creates an isolated workspace
    3. Invokes deps.coding_tool to execute the task
    4. Stores workspace_path in state for the commit node's scope check
    5. Updates task status based on tool result

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def execute_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        task_ordering = state.get("task_ordering", [])
        current_task_index = state.get("current_task_index", 0)
        tasks = list(state.get("tasks", []))
        errors = list(state.get("errors", []))

        # Get current task ID from ordering
        current_task_id = task_ordering[current_task_index] if task_ordering else None

        if current_task_id is None:
            node_path.append("execute")
            return {"node_path": node_path}

        # Find the task object for description
        task_description = ""
        task_target_files: list[str] = []
        for task in tasks:
            task_id = task.id if isinstance(task, Task) else task.get("id", "")
            if task_id == current_task_id:
                task_description = (
                    task.description if isinstance(task, Task)
                    else task.get("description", task.get("title", ""))
                )
                task_target_files = (
                    task.target_files if isinstance(task, Task)
                    else task.get("target_files", [])
                )
                break

        if not task_description:
            task_description = f"Execute task {current_task_id}"

        # Create isolated workspace for the task
        workspace_info = await deps.workspace_manager.create(
            task_id=current_task_id,
            session_id=session_id,
        )

        # Emit task start event
        start_event = Event.create(
            type=EventType.TASK_START,
            session_id=session_id,
            source="execute_node",
            payload={
                "task_id": current_task_id,
                "workspace_id": workspace_info.workspace_id,
                "workspace_path": workspace_info.path,
            },
            correlation_id=session_id,
            event_id=f"task-start-{current_task_id}",
        )
        await deps.event_bus.publish(start_event)

        # ─── Invoke the coding tool ──────────────────────────────────────
        tool_success = False
        if deps.coding_tool is not None:
            try:
                result = await deps.coding_tool.execute(
                    task_description=task_description,
                    workspace_path=workspace_info.path,
                )
                tool_success = result.success

                if not result.success:
                    logger.warning(
                        "Coding tool failed for task %s: %s",
                        current_task_id,
                        result.error,
                    )
                    errors.append({
                        "code": "tool_execution_failed",
                        "message": result.error,
                        "node": "execute",
                        "task_id": current_task_id,
                    })
            except Exception as exc:
                logger.exception("Coding tool raised for task %s", current_task_id)
                errors.append({
                    "code": "tool_exception",
                    "message": str(exc),
                    "node": "execute",
                    "task_id": current_task_id,
                })
        else:
            # No coding tool available — log and continue (DEGRADED mode)
            logger.warning(
                "No coding tool configured — task %s executed as no-op",
                current_task_id,
            )
            tool_success = True  # Allow verification to proceed

        # Update task status
        new_status = "verifying" if tool_success else "failed"
        for i, task in enumerate(tasks):
            task_id = task.id if isinstance(task, Task) else task.get("id", "")
            if task_id == current_task_id:
                if isinstance(task, Task):
                    tasks[i] = Task(
                        id=task.id,
                        title=task.title,
                        description=task.description,
                        depends_on=task.depends_on,
                        target_files=task.target_files,
                        status=new_status,
                        attempts=task.attempts + 1,
                        assigned_tool=task.assigned_tool,
                        workspace_id=workspace_info.workspace_id,
                    )
                break

        node_path.append("execute")
        return {
            "current_task_id": current_task_id,
            "workspace_path": workspace_info.path,
            "allowed_paths": task_target_files or None,
            "tasks": tasks,
            "errors": errors,
            "node_path": node_path,
        }

    return execute_node
