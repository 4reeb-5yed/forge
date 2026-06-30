"""Plan node — topological ordering of tasks by dependency.

Requirements: 7.1, 7.2, 7.3
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState, Task
from app.runtime.planner import plan
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_plan_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the plan node function.

    The plan node delegates to the planner module for topological sorting,
    sets task_ordering and initializes current_task_index to 0. On cycle
    detection, sets status to "plan_failed" and populates errors.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def plan_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        errors = list(state.get("errors", []))
        tasks = state.get("tasks", [])

        # Convert Task objects to dicts for the planner
        task_dicts = []
        for t in tasks:
            if isinstance(t, Task):
                task_dicts.append({
                    "id": t.id,
                    "depends_on": t.depends_on,
                })
            else:
                task_dicts.append(t)

        # Delegate to planner for topological ordering
        result = plan(task_dicts)

        node_path.append("plan")

        if not result.is_ok:
            # Cycle or unresolved dependency detected
            for err in result.errors:
                errors.append({
                    "code": "cycle_detected" if "cycle" in err.kind.value else "unresolved_dependency",
                    "message": err.message,
                    "task_ids": err.task_ids,
                    "node": "plan",
                })
            return {
                "status": "plan_failed",
                "errors": errors,
                "node_path": node_path,
            }

        return {
            "task_ordering": result.ordering,
            "current_task_index": 0,
            "node_path": node_path,
        }

    return plan_node
