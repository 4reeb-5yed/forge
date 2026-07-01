"""Graph builder — constructs the Forge LangGraph state machine.

Registers all 13 node functions, wires linear and conditional edges,
and returns a compiled graph ready for invocation.

Requirements: 14.1, 14.2, 14.3, 14.4
"""

from __future__ import annotations

from typing import Any

from app.runtime.models import ForgeState
from app.workflow.deps import RuntimeDeps
from app.workflow.nodes import (
    make_architect_node,
    make_clarify_node,
    make_classify_node,
    make_commit_node,
    make_doc_update_node,
    make_execute_node,
    make_finalize_node,
    make_intake_node,
    make_interrupt_node,
    make_plan_node,
    make_policy_node,
    make_status_node,
    make_verify_node,
)
from app.workflow.routing import (
    route_after_classify,
    route_after_commit,
    route_after_policy,
    route_after_verify,
)

try:
    from langgraph.graph import END, StateGraph

    _LANGGRAPH_AVAILABLE = True
except ImportError:
    _LANGGRAPH_AVAILABLE = False
    END = "__end__"
    StateGraph = None  # type: ignore[assignment, misc]


def build_forge_graph(
    deps: RuntimeDeps,
    enable_checkpointing: bool = True,
) -> Any:
    """Build and compile the LangGraph workflow.

    Registers all 13 nodes, sets intake as the entry point,
    wires linear and conditional edges, and returns the compiled graph.

    Args:
        deps: The RuntimeDeps container with all runtime components.
        enable_checkpointing: Whether to enable automatic checkpointing.

    Returns:
        A compiled LangGraph StateGraph (CompiledStateGraph) ready for
        invocation via graph.ainvoke(state).

    Raises:
        ImportError: If langgraph is not installed.
    """
    if not _LANGGRAPH_AVAILABLE:
        raise ImportError(
            "langgraph is required to build the workflow graph. "
            "Install it with: pip install langgraph>=0.2.0"
        )

    graph = StateGraph(ForgeState)

    # Initialize checkpoint middleware if enabled
    checkpoint_middleware = None
    if enable_checkpointing:
        try:
            from app.workflow.checkpoint_middleware import create_checkpoint_middleware
            # Use the recovery checkpoint store if available
            if deps.recovery and hasattr(deps.recovery, '_store'):
                checkpoint_middleware = create_checkpoint_middleware(deps.recovery._store)
            else:
                checkpoint_middleware = create_checkpoint_middleware(None)
        except ImportError:
            pass

    # Helper to wrap nodes with checkpoint middleware
    def add_node_with_checkpoint(name: str, node_fn: Any) -> None:
        if checkpoint_middleware is not None:
            wrapped = checkpoint_middleware.wrap_node(name, node_fn)
            graph.add_node(name, wrapped)
        else:
            graph.add_node(name, node_fn)

    # Register all 13 nodes
    add_node_with_checkpoint("intake", make_intake_node(deps))
    add_node_with_checkpoint("classify", make_classify_node(deps))
    add_node_with_checkpoint("clarify", make_clarify_node(deps))
    add_node_with_checkpoint("architect", make_architect_node(deps))
    add_node_with_checkpoint("plan", make_plan_node(deps))
    add_node_with_checkpoint("execute", make_execute_node(deps))
    add_node_with_checkpoint("verify", make_verify_node(deps))
    add_node_with_checkpoint("policy", make_policy_node(deps))
    add_node_with_checkpoint("commit", make_commit_node(deps))
    add_node_with_checkpoint("doc_update", make_doc_update_node(deps))
    add_node_with_checkpoint("finalize", make_finalize_node(deps))
    add_node_with_checkpoint("status", make_status_node(deps))
    add_node_with_checkpoint("interrupt", make_interrupt_node(deps))

    # Set entry point
    graph.set_entry_point("intake")

    # Linear edges
    graph.add_edge("intake", "classify")
    graph.add_edge("clarify", "architect")
    graph.add_edge("architect", "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("doc_update", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("status", END)
    graph.add_edge("interrupt", END)

    # Conditional edges
    graph.add_conditional_edges("classify", route_after_classify)
    graph.add_conditional_edges("execute", lambda s: "verify")
    graph.add_conditional_edges("verify", route_after_verify)
    graph.add_conditional_edges("policy", route_after_policy)
    graph.add_conditional_edges("commit", route_after_commit)

    return graph.compile()
