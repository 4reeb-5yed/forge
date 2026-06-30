"""Classify node — deterministic intent classification.

Requirements: 4.1, 4.2, 4.3
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.runtime.classifier import BuildState, classify
from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_classify_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the classify node function.

    The classify node delegates to the deterministic fast-path classifier
    and sets the `intent` field in state. No AI model is invoked for
    status_query or interrupt intents.

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def classify_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        message = state.get("message", "")
        status = state.get("status", "")

        # Build the build state for context-aware classification
        is_build_active = status in ("processing", "executing", "verifying")
        is_paused = status == "interrupted"
        build_state = BuildState(
            is_build_active=is_build_active,
            is_paused=is_paused,
        )

        # Delegate to the deterministic classifier
        result = classify(message, build_state)

        # Map classifier intent classes to workflow intent strings
        intent_mapping = {
            "interrupt": "interrupt",
            "pause": "interrupt",
            "resume": "interrupt",
            "stop": "interrupt",
            "redirect": "interrupt",
            "status_query": "status_query",
            "build_intent": "build_intent",
            "needs_ai_classification": "natural_language",
            "classification_unavailable": "natural_language",
        }
        intent = intent_mapping.get(result.intent.value, "natural_language")

        node_path.append("classify")
        return {
            "intent": intent,
            "node_path": node_path,
        }

    return classify_node
