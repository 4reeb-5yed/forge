"""Conditional edge routing functions for the Forge LangGraph workflow.

All routing functions are pure functions on ForgeState — no side effects,
no external calls. They determine the next node based on state fields.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7
"""

from __future__ import annotations

from app.runtime.models import ForgeState

# Sentinel for LangGraph END (string used by LangGraph)
_END = "__end__"


def route_after_classify(state: ForgeState) -> str:
    """Route based on classified intent.

    Returns:
        "clarify" for build_intent
        "status" for status_query
        "interrupt" for interrupt
        END for unknown/natural_language intents
    """
    intent = state.get("intent", "")
    if intent == "build_intent":
        return "clarify"
    elif intent == "status_query":
        return "status"
    elif intent == "interrupt":
        return "interrupt"
    else:
        return _END


def route_after_verify(state: ForgeState) -> str:
    """Route based on verification outcome.

    Returns:
        "commit" if verification passed for the current task
        "policy" if verification failed
    """
    results = state.get("verification_results", {})
    current_task = state.get("current_task_id")

    if current_task and results.get(current_task, {}).get("passed", False):
        return "commit"
    else:
        return "policy"


def route_after_policy(state: ForgeState) -> str:
    """Route after policy decision.

    Policy always routes back to execute — the policy node modifies state
    to handle retry (reset task), skip (advance index), or escalate (swap tool).

    Returns:
        "execute" always
    """
    return "execute"


def route_after_commit(state: ForgeState) -> str:
    """Route after commit: loop or finalize.

    Returns:
        "doc_update" if all tasks are done
        "execute" if more tasks remain
    """
    if state.get("all_tasks_done", False):
        return "doc_update"
    else:
        return "execute"
