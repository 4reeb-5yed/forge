"""Status node — delegates to the RuntimeInspector for status queries.

Requirements: 13.2
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_status_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the status node function.

    The status node delegates to deps.inspector.get_status() and returns
    the status summary in state.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def status_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")

        # Delegate to inspector
        _status = deps.inspector.get_status(session_id)

        node_path.append("status")
        return {
            "node_path": node_path,
        }

    return status_node
