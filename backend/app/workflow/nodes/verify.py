"""Verify node — runs the verification pipeline for the current task.

Requirements: 9.1, 9.2, 9.3
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState
from app.runtime.verification import VerificationPipeline
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_verify_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the verify node function.

    The verify node constructs a VerificationPipeline for the current task,
    runs the pipeline, and records results in verification_results.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def verify_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        current_task_id = state.get("current_task_id")
        verification_results = dict(state.get("verification_results", {}))

        if current_task_id is None:
            node_path.append("verify")
            return {"node_path": node_path}

        # Construct verification pipeline for the current task
        # In production, deps would have a verification_pipeline_factory
        # For now, construct a minimal pipeline
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[],
            event_emitter=deps.event_bus.publish,
            session_id=session_id,
            task_id=current_task_id,
        )

        # Run the pipeline
        result = await pipeline.run()

        # Record results
        verification_results[current_task_id] = {
            "passed": result.all_blocking_passed,
            "advisory": {
                name: {"status": sr.status.value, "detail": sr.detail}
                for name, sr in result.advisory_results.items()
            },
            "blocking": {
                name: {"status": sr.status.value, "detail": sr.detail}
                for name, sr in result.blocking_results.items()
            },
            "halted_at": result.halted_at,
        }

        node_path.append("verify")
        return {
            "verification_results": verification_results,
            "node_path": node_path,
        }

    return verify_node
