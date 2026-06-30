"""Commit node — commits verified changes and advances task index.

Requirements: 10.1, 10.2, 10.3
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_commit_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the commit node function.

    The commit node commits changes via workspace merge (placeholder SHA
    generation for now), appends the SHA to commit_shas, increments
    current_task_index, and sets all_tasks_done when complete.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def commit_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        commit_shas = list(state.get("commit_shas", []))
        current_task_index = state.get("current_task_index", 0)
        task_ordering = state.get("task_ordering", [])
        current_task_id = state.get("current_task_id")

        # Generate a placeholder commit SHA
        commit_sha = uuid.uuid4().hex[:12]
        commit_shas.append(commit_sha)

        # Increment task index
        current_task_index += 1

        # Determine if all tasks are done
        all_tasks_done = current_task_index >= len(task_ordering)

        # Emit commit.done event
        commit_event = Event.create(
            type=EventType.COMMIT_DONE,
            session_id=session_id,
            source="commit_node",
            payload={
                "sha": commit_sha,
                "task_id": current_task_id,
                "task_index": current_task_index - 1,
            },
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await deps.event_bus.publish(commit_event)

        node_path.append("commit")
        return {
            "commit_shas": commit_shas,
            "current_task_index": current_task_index,
            "all_tasks_done": all_tasks_done,
            "node_path": node_path,
        }

    return commit_node
