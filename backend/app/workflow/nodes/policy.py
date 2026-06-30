"""Policy node — decides retry/skip/escalate on verification failure.

Requirements: 13.5
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState, Task
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_policy_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the policy node function.

    The policy node delegates to deps.policy_engine.decide() to determine
    whether to retry, skip, or escalate a failed verification stage.
    Records the decision in the decisions list.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def policy_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        decisions = list(state.get("decisions", []))
        current_task_id = state.get("current_task_id", "")
        verification_results = state.get("verification_results", {})
        tasks = state.get("tasks", [])

        # Determine the failing stage from verification results
        task_results = verification_results.get(current_task_id, {})
        halted_at = task_results.get("halted_at", "unknown")

        # Determine attempt count for this task
        attempt_count = 1
        for t in tasks:
            task_id = t.id if isinstance(t, Task) else t.get("id", "")
            if task_id == current_task_id:
                attempt_count = t.attempts if isinstance(t, Task) else t.get("attempts", 1)
                break

        # Delegate to policy engine
        result = await deps.policy_engine.decide(
            task_id=current_task_id or "",
            stage_name=halted_at or "unknown",
            attempt_count=attempt_count,
        )

        # Record decision ID
        decisions.append(f"{result.decision.value}:{current_task_id}:{halted_at}")

        node_path.append("policy")
        return {
            "decisions": decisions,
            "node_path": node_path,
        }

    return policy_node
