"""Node function factories for the Forge LangGraph workflow.

Each node function follows the factory pattern: make_X_node(deps) returns
an async function that accepts ForgeState and returns a partial dict of
state updates. Nodes are thin wrappers delegating to existing runtime components.
"""

from app.workflow.nodes.architect import make_architect_node
from app.workflow.nodes.clarify import make_clarify_node
from app.workflow.nodes.classify import make_classify_node
from app.workflow.nodes.commit import make_commit_node
from app.workflow.nodes.doc_update import make_doc_update_node
from app.workflow.nodes.execute import make_execute_node
from app.workflow.nodes.finalize import make_finalize_node
from app.workflow.nodes.intake import make_intake_node
from app.workflow.nodes.interrupt import make_interrupt_node
from app.workflow.nodes.plan import make_plan_node
from app.workflow.nodes.policy import make_policy_node
from app.workflow.nodes.status import make_status_node
from app.workflow.nodes.verify import make_verify_node

__all__ = [
    "make_architect_node",
    "make_clarify_node",
    "make_classify_node",
    "make_commit_node",
    "make_doc_update_node",
    "make_execute_node",
    "make_finalize_node",
    "make_intake_node",
    "make_interrupt_node",
    "make_plan_node",
    "make_policy_node",
    "make_status_node",
    "make_verify_node",
]
