"""Clarify node — gathers missing specification inputs via clarification questions.

Requirements: 5.1, 5.2, 5.3
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.clarification import (
    SessionContext,
    get_missing_inputs,
    run_clarification,
)
from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_clarify_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the clarify node function.

    The clarify node checks if all required specification inputs are present.
    If inputs are missing, it emits clarification question events and sets
    needs_clarification to True. Otherwise, it sets needs_clarification to False.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def clarify_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")

        # Build a SessionContext from state (or use a minimal one)
        context = SessionContext(session_id=session_id)

        # Check for missing inputs
        missing = get_missing_inputs(context)

        if not missing:
            # All inputs present — advance without clarification
            node_path.append("clarify")
            return {
                "needs_clarification": False,
                "node_path": node_path,
            }

        # Emit clarification question events for missing inputs
        await run_clarification(
            context,
            deps.event_bus.publish,
            session_id=session_id,
        )

        node_path.append("clarify")
        return {
            "needs_clarification": True,
            "node_path": node_path,
        }

    return clarify_node
