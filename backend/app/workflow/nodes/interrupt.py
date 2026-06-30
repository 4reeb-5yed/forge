"""Interrupt node — pauses execution via the InterruptHandler.

Requirements: 13.3
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_interrupt_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the interrupt node function.

    The interrupt node delegates to deps.interrupt_handler.pause() to
    pause execution and set status to "interrupted".

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def interrupt_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        message = state.get("message", "")

        # Delegate to interrupt handler
        await deps.interrupt_handler.pause(
            session_id=session_id,
            message=message,
            build_state=dict(state),
        )

        node_path.append("interrupt")
        return {
            "status": "interrupted",
            "node_path": node_path,
        }

    return interrupt_node
