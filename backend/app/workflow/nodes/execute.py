"""Execute node — dispatches current task in an isolated workspace.

Requirements: 8.1, 8.2, 8.3
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState, Task
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_execute_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the execute node function.

    The execute node gets the current task from task_ordering, creates
    an isolated workspace, dispatches execution, and updates task status.

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

        # Get current task ID from ordering
        current_task_id = task_ordering[current_task_index] if task_ordering else None

        if current_task_id is None:
            node_path.append("execute")
            return {"node_path": node_path}

        # Create isolated workspace for the task
        workspace_info = await deps.workspace_manager.create(
            task_id=current_task_id,
            session_id=session_id,
        )

        # Update the current task status to "running" then "verifying"
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
                        status="verifying",
                        attempts=task.attempts + 1,
                        assigned_tool=task.assigned_tool,
                        workspace_id=workspace_info.workspace_id,
                    )
                break

        node_path.append("execute")
        return {
            "current_task_id": current_task_id,
            "tasks": tasks,
            "node_path": node_path,
        }

    return execute_node
