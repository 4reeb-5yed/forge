"""RuntimeDeps — dependency container for all Forge runtime components.

Assembled once at application startup and passed into node function factories
via closure capture. Contains no business logic — pure wiring container.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.runtime.audit import AuditTrail
from app.runtime.budget import SessionBudget
from app.runtime.events.bus import EventBus
from app.runtime.health import HealthMonitor
from app.runtime.inspector import RuntimeInspector
from app.runtime.interrupt import InterruptHandler
from app.runtime.learning import LearningRecorder
from app.runtime.mode import ModeEvaluator
from app.runtime.policies import PolicyEngine
from app.runtime.recovery import CrashRecovery
from app.runtime.registry import CapabilityRegistry
from app.runtime.router import ModelRouter
from app.runtime.secrets import SecretHolder
from app.runtime.session import SessionManager
from app.runtime.workspace import WorkspaceManager


@dataclass
class RuntimeDeps:
    """All runtime component instances, assembled at startup.

    Passed into node function factories via closure so that each node
    can delegate to the appropriate component without global state.

    This container is instantiated once per process lifetime. It holds
    references — it does not own the lifecycle of any component (that's
    managed by the bootstrap/shutdown sequence).
    """

    # Core infrastructure
    event_bus: EventBus
    registry: CapabilityRegistry
    secret_holder: SecretHolder

    # Session & state
    session_manager: SessionManager
    audit_trail: AuditTrail
    recovery: CrashRecovery

    # AI routing
    model_router: ModelRouter

    # Workspace & dispatch
    workspace_manager: WorkspaceManager

    # Inspection & control
    inspector: RuntimeInspector
    interrupt_handler: InterruptHandler
    mode_evaluator: ModeEvaluator

    # Policy & learning
    policy_engine: PolicyEngine
    learning_recorder: LearningRecorder

    # Health
    health_monitor: HealthMonitor

    # Budget factory: creates a SessionBudget per session
    budget_factory: Callable[[str], SessionBudget] = field(
        default_factory=lambda: _default_budget_factory
    )

    # Coding tool (SandboxedAiderTool or AiderTool, wired during bootstrap)
    coding_tool: Any = None

    # Configuration paths (for bootstrap)
    config_dir: str = "config"


def _default_budget_factory(session_id: str) -> SessionBudget:
    """Default budget factory — 1M tokens per session."""
    return SessionBudget(token_limit=1_000_000, session_id=session_id)
