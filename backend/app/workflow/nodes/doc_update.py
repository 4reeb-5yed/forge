"""Doc update node — updates documentation after commits.

Requirements: 11.1, 11.2, 11.3
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

logger = logging.getLogger(__name__)

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_doc_update_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the doc_update node function.

    The doc_update node invokes the DocWriter to update documentation files.
    Handles graceful degradation if the DocWriter capability is unavailable,
    recording a documentation drift warning without failing.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def doc_update_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        doc_updates: list[str] = []

        # Placeholder: In production, this would invoke the DocWriter capability
        # via deps to update documentation files based on commit diffs.
        # For now, populate doc_updates as empty (graceful degradation).
        try:
            # Attempt to invoke DocWriter (placeholder — no real implementation yet)
            # If DocWriter is unavailable, graceful degradation applies
            doc_updates = []
        except Exception as exc:
            # Graceful degradation: record drift warning, don't fail
            logger.warning(
                "DocWriter unavailable, recording documentation drift: %s", exc
            )

        node_path.append("doc_update")
        return {
            "doc_updates": doc_updates,
            "node_path": node_path,
        }

    return doc_update_node
