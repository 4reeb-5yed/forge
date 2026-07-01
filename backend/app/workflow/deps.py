"""RuntimeDeps — dependency container for all Forge runtime components.

Assembled once at application startup and passed into node function factories
via closure capture. Contains no business logic — pure wiring container.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

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

DEFAULT_TOKEN_LIMIT = 1_000_000  # Default: 1M tokens per session


def load_rate_limits_config(config_dir: str) -> dict[str, Any]:
    """Load rate limits configuration from rate_limits.yaml.

    Parses the YAML file and returns the session config dict. Returns a default
    config with 1M token limit if the file is not found or cannot be parsed.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        The 'session' dict from rate_limits.yaml with 'max_tokens' and 'max_cost_usd'.
    """
    rate_limits_path = Path(config_dir) / "rate_limits.yaml"
    if not rate_limits_path.exists():
        return {"max_tokens": DEFAULT_TOKEN_LIMIT, "max_cost_usd": 50.00}

    try:
        with open(rate_limits_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data and "session" in data:
            return data["session"]
        return {"max_tokens": DEFAULT_TOKEN_LIMIT, "max_cost_usd": 50.00}
    except (yaml.YAMLError, OSError):
        return {"max_tokens": DEFAULT_TOKEN_LIMIT, "max_cost_usd": 50.00}


def create_budget_factory(config_dir: str) -> Callable[[str], SessionBudget]:
    """Create a budget factory that loads config from rate_limits.yaml.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        A callable that creates SessionBudget instances with the configured token limit.
    """
    rate_config = load_rate_limits_config(config_dir)
    token_limit = rate_config.get("max_tokens", DEFAULT_TOKEN_LIMIT)

    def budget_factory(session_id: str) -> SessionBudget:
        return SessionBudget(token_limit=token_limit, session_id=session_id)

    return budget_factory


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
    """Default budget factory — loads from rate_limits.yaml or uses 1M tokens."""
    token_limit = os.environ.get("FORGE_TOKEN_LIMIT")
    if token_limit:
        try:
            return SessionBudget(token_limit=int(token_limit), session_id=session_id)
        except ValueError:
            pass
    return SessionBudget(token_limit=DEFAULT_TOKEN_LIMIT, session_id=session_id)
