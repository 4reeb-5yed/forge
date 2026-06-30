"""Intake node — validates incoming message and initializes workflow state.

Requirements: 3.1, 3.2, 3.3, 2.1, 2.2, 2.3
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_intake_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the intake node function.

    The intake node validates the incoming message, sets status to "processing",
    writes a checkpoint via CrashRecovery, and handles invalid input by
    appending an error and setting status to "failed".

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def intake_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        errors = list(state.get("errors", []))
        session_id = state.get("session_id", "")
        message = state.get("message", "")

        # Validate message is non-empty
        if not message or not message.strip():
            errors.append({
                "code": "invalid_input",
                "message": "Message field is empty or missing",
                "node": "intake",
            })
            node_path.append("intake")
            return {
                "status": "failed",
                "errors": errors,
                "node_path": node_path,
            }

        # Valid input — set processing status
        node_path.append("intake")

        # Write checkpoint via CrashRecovery
        try:
            await deps.recovery.checkpoint_after_node(
                session_id=session_id,
                node_id="intake",
                state=dict(state),
                highest_seq=0,
            )
        except Exception:
            # Checkpoint failure is non-fatal for the node itself;
            # CrashRecovery handles halting via its own error emission
            pass

        return {
            "status": "processing",
            "node_path": node_path,
        }

    return intake_node
