"""Architect node — invokes the Architect role to produce spec + task list.

Requirements: 6.1, 6.2, 6.3
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState, Role, Task
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_architect_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the architect node function.

    The architect node invokes the Architect role via the model router,
    parses the response into a specification artifact URI and task list,
    and emits spec.ready and tasks.ready events.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def architect_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        message = state.get("message", "")

        # Build messages for the Architect role
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Architect role in the Forge build system. "
                    "Generate a specification and task list for the given request. "
                    "Return JSON with keys 'specification' (string) and "
                    "'tasks' (list of objects with 'id', 'title', 'description', 'depends_on')."
                ),
            },
            {"role": "user", "content": message},
        ]

        # Invoke Architect role via model router
        completion = await deps.model_router.route(
            Role.ARCHITECT, messages, estimated_tokens=4000
        )

        # Parse the response — attempt JSON, fall back to plain text
        import json

        try:
            data = json.loads(completion)
            spec_content = data.get("specification", completion)
            task_list = data.get("tasks", [])

            # Ensure all task IDs and dependencies are strings (required by Task model)
            for t in task_list:
                if "id" in t and not isinstance(t["id"], str):
                    t["id"] = str(t["id"])
                if "depends_on" in t:
                    t["depends_on"] = [str(x) for x in t["depends_on"]]

        except (json.JSONDecodeError, TypeError) as exc:
            spec_content = completion
            task_list = [
                {"id": "task_1", "title": "Implement specification", "description": message, "depends_on": []}
            ]

        # Store spec artifact URI (placeholder — in production this saves to artifact store)
        spec_artifact_uri = f"artifact://{session_id}/spec/v1"

        # Convert task dicts to Task objects
        tasks = [
            Task(
                id=t.get("id", f"task_{i}"),
                title=t.get("title", "Untitled"),
                description=t.get("description", ""),
                depends_on=t.get("depends_on", []),
            )
            for i, t in enumerate(task_list)
        ]

        # Emit spec.ready event
        spec_event = Event.create(
            type=EventType.SPEC_READY,
            session_id=session_id,
            source="architect_node",
            payload={"uri": spec_artifact_uri, "version": 1},
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await deps.event_bus.publish(spec_event)

        # Emit tasks.ready event
        tasks_event = Event.create(
            type=EventType.TASKS_READY,
            session_id=session_id,
            source="architect_node",
            payload={"task_count": len(tasks), "graph_ref": f"task_graph:{session_id}:v1"},
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await deps.event_bus.publish(tasks_event)

        node_path.append("architect")
        return {
            "spec_artifact_uri": spec_artifact_uri,
            "tasks": tasks,
            "node_path": node_path,
        }

    return architect_node
