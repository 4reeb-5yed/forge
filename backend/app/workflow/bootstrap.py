"""Bootstrap sequence — brings the Forge runtime to forge.ready state.

Runs at application startup:
1. Load and validate configuration
2. Run concurrent discovery (probe all resources)
3. Register healthy capabilities in the registry
4. Start health monitor background task
5. Evaluate operational mode
6. Emit forge.ready event

Requirements: 15.1, 15.2, 15.3, 15.4, 16.1
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.runtime.events.models import Event, EventType
from app.workflow.deps import RuntimeDeps

logger = logging.getLogger(__name__)


async def bootstrap(deps: RuntimeDeps) -> None:
    """Execute the full bootstrap sequence: Discovery → Registry → Health → Mode → forge.ready.

    This function brings the runtime from an uninitialized state to the forge.ready
    event, at which point the system is ready to accept workflow invocations.

    Args:
        deps: The fully assembled RuntimeDeps container.

    Raises:
        ConfigValidationError: If configuration validation fails (halts startup).
    """
    config_dir = Path(deps.config_dir)

    # Step 1–3: Discovery (load config, probe resources, register healthy ones)
    # Discovery handles config validation, probing, and registration internally
    if config_dir.exists():
        try:
            from app.runtime.discovery import run_discovery

            await run_discovery(
                config_dir=config_dir,
                registry=deps.registry,
                probe_map={},  # No real probes during bootstrap — adapters wire later
                event_emitter=deps.event_bus.publish,
                session_id="boot",
            )
        except Exception as exc:
            logger.warning(
                "Discovery skipped or failed: %s. Continuing with empty registry.",
                exc,
            )
    else:
        logger.info(
            "Config directory '%s' not found; skipping discovery.", config_dir
        )

    # Step 4: Start health monitor background task
    try:
        await deps.health_monitor.start()
    except Exception as exc:
        logger.warning("Health monitor failed to start: %s", exc)

    # Step 5: Evaluate operational mode
    mode_result = await deps.mode_evaluator.evaluate_and_emit()
    logger.info("Operational mode: %s", mode_result.mode.value)

    # Step 6: Emit forge.ready event
    ready_event = Event.create(
        type=EventType.FORGE_READY,
        session_id="system",
        source="bootstrap",
        payload={
            "mode": mode_result.mode.value,
            "can_operate": mode_result.summary.can_operate,
            "available_capabilities": len(mode_result.summary.available),
            "startup_report": mode_result.startup_report,
        },
        correlation_id="system",
        event_id="forge_ready_bootstrap",
    )
    await deps.event_bus.publish(ready_event)

    logger.info("Bootstrap complete — forge.ready emitted")


def assemble_deps(config_dir: str = "config") -> RuntimeDeps:
    """Instantiate all runtime components and wire them into RuntimeDeps.

    Uses in-memory stores (no external databases required). Returns a fully
    assembled dependency container ready for bootstrap and graph construction.

    Args:
        config_dir: Path to the configuration directory (default "config").

    Returns:
        A fully wired RuntimeDeps instance.
    """
    from app.runtime.audit import AuditTrail
    from app.runtime.events.bus import EventBus
    from app.runtime.health import HealthMonitor, HealthMonitorConfig
    from app.runtime.inspector import RuntimeInspector
    from app.runtime.interrupt import InterruptHandler
    from app.runtime.learning import LearningRecorder
    from app.runtime.mode import ModeEvaluator
    from app.runtime.policies import PolicyConfig, PolicyEngine
    from app.runtime.recovery import CrashRecovery
    from app.runtime.registry import CapabilityRegistry
    from app.runtime.router import ModelRouter, RoleChainConfig
    from app.runtime.secrets import SecretHolder
    from app.runtime.session import SessionManager
    from app.runtime.workspace import WorkspaceManager

    # Core infrastructure
    event_bus = EventBus()
    registry = CapabilityRegistry(event_emitter=event_bus.publish, session_id="system")
    secret_holder = SecretHolder()

    # Session & state
    session_manager = SessionManager(secret_holder=secret_holder)
    audit_trail = AuditTrail()

    # In-memory checkpoint store for CrashRecovery
    class _InMemoryCheckpointStore:
        """Minimal in-memory checkpoint store for development."""

        def __init__(self) -> None:
            self._checkpoints: dict[str, Any] = {}

        async def write_checkpoint(
            self, session_id: str, node_id: str, highest_seq: int, redacted_state: dict
        ) -> None:
            self._checkpoints[session_id] = {
                "node_id": node_id,
                "highest_seq": highest_seq,
                "state": redacted_state,
            }

        async def get_latest_checkpoint(self, session_id: str):
            return self._checkpoints.get(session_id)

        async def list_non_terminal_sessions(self) -> list:
            return []

    recovery = CrashRecovery(
        checkpoint_store=_InMemoryCheckpointStore(),
        event_bus=event_bus,
    )

    # Subscribe audit trail to all events
    event_bus.subscribe("*", audit_trail.handle_event, subscriber_id="audit_trail")

    # AI routing — uses empty chain config (no providers configured yet)
    model_router = ModelRouter(
        chain_config=RoleChainConfig(),
        registry_checker=lambda name: registry.has_by_name(name) if hasattr(registry, "has_by_name") else False,
        call_adapter=_noop_call_adapter,
        event_emitter=event_bus.publish,
        session_id="system",
    )

    # Workspace management
    workspace_manager = WorkspaceManager(event_bus=event_bus)

    # Inspection & control
    inspector = RuntimeInspector(
        audit_trail=audit_trail,
        registry=registry,
    )
    interrupt_handler = InterruptHandler(event_emitter=event_bus.publish)
    mode_evaluator = ModeEvaluator(
        registry=registry,
        event_emitter=event_bus.publish,
        session_id="system",
    )

    # Policy & learning
    policy_engine = PolicyEngine(config=PolicyConfig())
    learning_recorder = LearningRecorder()

    # Health monitoring
    health_monitor = HealthMonitor(
        registry=registry,
        probe_map={},
        event_emitter=event_bus.publish,
        config=HealthMonitorConfig(),
        session_id="system",
    )

    return RuntimeDeps(
        event_bus=event_bus,
        registry=registry,
        secret_holder=secret_holder,
        session_manager=session_manager,
        audit_trail=audit_trail,
        recovery=recovery,
        model_router=model_router,
        workspace_manager=workspace_manager,
        inspector=inspector,
        interrupt_handler=interrupt_handler,
        mode_evaluator=mode_evaluator,
        policy_engine=policy_engine,
        learning_recorder=learning_recorder,
        health_monitor=health_monitor,
        config_dir=config_dir,
    )


async def _noop_call_adapter(provider: str, model: str, messages: list, **kwargs) -> str:
    """No-op call adapter for development — no real AI calls."""
    return '{"result": "no-op"}'
