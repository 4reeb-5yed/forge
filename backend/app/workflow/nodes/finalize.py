"""Finalize node — completes the build with VCS push and learning record.

Requirements: 12.1, 12.2, 12.3
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_finalize_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the finalize node function.

    The finalize node sets status to "completed", emits a build.done event,
    and records learning outcomes via the LearningRecorder.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def finalize_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        commit_shas = state.get("commit_shas", [])

        # Emit build.done event
        build_event = Event.create(
            type=EventType.BUILD_DONE,
            session_id=session_id,
            source="finalize_node",
            payload={
                "status": "completed",
                "commit_count": len(commit_shas),
                "commit_shas": list(commit_shas),
            },
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await deps.event_bus.publish(build_event)

        # Record learning outcomes via LearningRecorder
        tasks = state.get("tasks", [])
        task_results = []
        for t in tasks:
            task_id = t.id if hasattr(t, "id") else t.get("id", "")
            task_status = t.status if hasattr(t, "status") else t.get("status", "")
            task_results.append({
                "task_id": task_id,
                "task_type": "build",
                "tool": "unknown",
                "model": "unknown",
                "role": "coder",
                "outcome_status": "success" if task_status == "committed" else "failure",
                "retry_count": 0,
                "escalation_flag": False,
            })

        if task_results:
            await deps.learning_recorder.record_build_outcomes(session_id, task_results)

        node_path.append("finalize")
        return {
            "status": "completed",
            "node_path": node_path,
        }

    return finalize_node
