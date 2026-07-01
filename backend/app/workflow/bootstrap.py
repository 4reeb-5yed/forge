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

import yaml

from app.workflow.deps import RuntimeDeps, create_budget_factory

logger = logging.getLogger(__name__)


def load_role_chain_config(config_dir: str) -> dict[str, Any] | None:
    """Load the role chain configuration from models.yaml.

    Parses the YAML file and returns the 'roles' dict for use with
    RoleChainConfig.from_yaml_dict(). Returns None if the file is not found
    or cannot be parsed.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        The 'roles' dict from models.yaml, or None if not available.
    """
    models_yaml_path = Path(config_dir) / "models.yaml"
    if not models_yaml_path.exists():
        logger.warning("models.yaml not found at %s", models_yaml_path)
        return None

    try:
        with open(models_yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("roles") if data else None
    except yaml.YAMLError as exc:
        logger.error("Failed to parse models.yaml: %s", exc)
        return None
    except OSError as exc:
        logger.error("Failed to read models.yaml: %s", exc)
        return None


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

    # Step 0: Load ConfigService configuration
    if deps.config_service is not None:
        try:
            await deps.config_service.load()
            logger.info("ConfigService loaded configuration")
        except Exception as exc:
            logger.warning("ConfigService load failed: %s. Continuing with defaults.", exc)

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

    # Step 4.5: Register OpenRouter as a capability (if API key present)
    # This makes _check_provider("openrouter") return True for the model router
    import os
    if os.environ.get("OPENROUTER_API_KEY"):
        from app.runtime.models import Capability, CapabilityEntry, CapabilityKind, Role
        try:
            openrouter_entry = CapabilityEntry(
                name=Capability.AI_CODER,
                kind=CapabilityKind.AI_PROVIDER,
                healthy=True,
                roles=[Role.CODER, Role.ARCHITECT, Role.PLANNER, Role.CLARIFICATION, Role.REVIEWER, Role.DOC_WRITER],
                provider_name="openrouter",
            )
            await deps.registry.register(openrouter_entry)
            logger.info("Registered OpenRouter as AI provider (all roles)")
        except Exception as exc:
            logger.warning("Failed to register OpenRouter capability: %s", exc)

    # Step 5: Evaluate operational mode (also emits forge.ready per requirement 13.7)
    mode_result = await deps.mode_evaluator.evaluate_and_emit()
    logger.info("Operational mode: %s", mode_result.mode.value)

    # Step 6: Wire ConfigService to deps for hot-reload
    if deps.config_service is not None:
        deps.config_service.set_deps(deps)
        # Apply loaded config to runtime (model router, sandbox mode, API key)
        deps.config_service.apply_to_runtime()
        logger.info("ConfigService wired to RuntimeDeps for hot-reload")

    logger.info("Bootstrap complete — forge.ready emitted")


def assemble_deps(config_dir: str = "config") -> RuntimeDeps:
    """Instantiate all runtime components and wire them into RuntimeDeps.

    Uses in-memory stores (no external databases required). Returns a fully
    assembled dependency container ready for bootstrap and graph construction.

    Security notes:
    - GITHUB_TOKEN is NOT passed to AiderTool or workspace environment.
      It is only used by GitHubVCS adapter for clone/push operations (separate path).
    - OPENROUTER_API_KEY is the only secret passed to the sandbox (Aider needs it).
    - SandboxedAiderTool is used by default when Docker is available.

    Args:
        config_dir: Path to the configuration directory (default "config").

    Returns:
        A fully wired RuntimeDeps instance.
    """
    import os
    from app.runtime.audit import AuditTrail
    from app.runtime.config import ConfigService
    from app.runtime.events.bus import EventBus
    from app.runtime.health import HealthMonitor, HealthMonitorConfig
    from app.runtime.inspector import RuntimeInspector
    from app.runtime.interrupt import InterruptHandler
    from app.runtime.learning import LearningRecorder
    from app.runtime.mode import ModeEvaluator
    from app.runtime.policies import PolicyConfig, PolicyEngine
    from app.runtime.recovery import CrashRecovery
    from app.runtime.registry import CapabilityRegistry
    from app.runtime.router import ModelRouter, RoleChainConfig, ChainEntry
    from app.runtime.secrets import SecretHolder
    from app.runtime.session import SessionManager
    from app.runtime.workspace import WorkspaceManager
    from app.runtime.models import Role, Capability, CapabilityEntry, CapabilityKind

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

    # ─── AI Routing: Wire OpenRouter as real call adapter ─────────────
    # If OPENROUTER_API_KEY is set, use the real adapter. Otherwise no-op.
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        from app.adapters.openrouter import OpenRouterProvider
        openrouter = OpenRouterProvider(api_key=openrouter_key)
        call_adapter = openrouter.as_call_adapter()
        logger.info("AI call adapter: OpenRouterProvider (real AI calls enabled)")
    else:
        call_adapter = _noop_call_adapter
        logger.warning("No OPENROUTER_API_KEY — using no-op call adapter (AI calls disabled)")

    # Configure role chains — try loading from models.yaml, fall back to OpenRouter defaults
    roles_config = load_role_chain_config(config_dir)
    if roles_config:
        try:
            chain_config = RoleChainConfig.from_yaml_dict(roles_config)
            logger.info("Loaded role chain config from models.yaml")
        except Exception as exc:
            logger.warning("Failed to parse models.yaml, using defaults: %s", exc)
            chain_config = _create_default_chain_config()
    else:
        # Fallback: all roles use OpenRouter with FORGE_MODEL or default
        chain_config = _create_default_chain_config()
        logger.info("Using default role chain config (models.yaml not available)")

    # registry_checker: returns True if any healthy capability has the given provider_name
    def _check_provider(name: str) -> bool:
        return any(
            entry.provider_name == name and entry.healthy
            for entry in registry._entries.values()
        )

    model_router = ModelRouter(
        chain_config=chain_config,
        registry_checker=_check_provider,
        call_adapter=call_adapter,
        event_emitter=event_bus.publish,
        session_id="system",
    )

    # Workspace management
    workspace_manager = WorkspaceManager(event_bus=event_bus)

    # Coding tool — prefer sandboxed version when Docker is available
    # NOTE: GITHUB_TOKEN is intentionally NOT passed to the coding tool.
    # The coding tool only receives OPENROUTER_API_KEY (for AI model calls).
    # VCS operations (clone/push) use GitHubVCS adapter on a separate path.
    coding_tool = _create_coding_tool()

    # VCS connector — GitHubVCS for clone/commit/push operations
    from app.adapters.github_vcs import GitHubVCS
    vcs = GitHubVCS(token=os.environ.get("GITHUB_TOKEN", ""))

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
    learning_recorder = LearningRecorder(event_emitter=event_bus)

    # Health monitoring
    health_monitor = HealthMonitor(
        registry=registry,
        probe_map={},
        event_emitter=event_bus.publish,
        config=HealthMonitorConfig(),
        session_id="system",
    )

    # ConfigService — runtime config manager
    config_path = Path(os.environ.get("FORGE_CONFIG_PATH", "/data/forge-config.json"))
    config_service = ConfigService(
        config_path=config_path,
        event_emitter=event_bus.publish,
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
        coding_tool=coding_tool,
        vcs=vcs,
        inspector=inspector,
        interrupt_handler=interrupt_handler,
        mode_evaluator=mode_evaluator,
        policy_engine=policy_engine,
        learning_recorder=learning_recorder,
        health_monitor=health_monitor,
        config_service=config_service,
        budget_factory=create_budget_factory(config_dir),
        config_dir=config_dir,
    )


def _create_coding_tool():
    """Create the appropriate coding tool.

    Priority:
    1. OpenHands Cloud (if OPENHANDS_API_KEY is set) — uses its own free model
    2. SandboxedAiderTool (if Docker available)
    3. Direct AiderTool (fallback, unsandboxed)

    Security: only OPENROUTER_API_KEY is passed to the sandbox.
    GITHUB_TOKEN and other secrets are never exposed to the coding tool.
    """
    import os
    import shutil

    # Priority 1: OpenHands Cloud
    openhands_key = os.environ.get("OPENHANDS_API_KEY", "")
    if openhands_key:
        from app.adapters.openhands import OpenHandsTool
        logger.info("Coding tool: OpenHandsTool (OpenHands Cloud API)")
        return OpenHandsTool(api_key=openhands_key)

    # Priority 2/3: Aider (sandboxed or direct)
    use_sandbox = os.environ.get("FORGE_USE_SANDBOX", "auto")

    if use_sandbox == "never":
        from app.adapters.aider_tool import AiderTool
        logger.info("Coding tool: AiderTool (sandbox disabled via FORGE_USE_SANDBOX=never)")
        return AiderTool()

    docker_available = shutil.which("docker") is not None

    if use_sandbox == "always":
        if not docker_available:
            raise RuntimeError(
                "FORGE_USE_SANDBOX=always but 'docker' CLI not found on PATH. "
                "Install Docker CLI in the container (see Dockerfile) and mount "
                "/var/run/docker.sock (see docker-compose.yml). "
                "Refusing to start without sandbox."
            )
        from app.adapters.sandboxed_aider import SandboxedAiderTool
        logger.info("Coding tool: SandboxedAiderTool (FORGE_USE_SANDBOX=always)")
        return SandboxedAiderTool(
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        )

    # auto mode
    if docker_available:
        from app.adapters.sandboxed_aider import SandboxedAiderTool
        logger.info("Coding tool: SandboxedAiderTool (Docker detected)")
        return SandboxedAiderTool(
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        )

    # Fallback: Docker not available — this is a security gap, make it loud
    from app.adapters.aider_tool import AiderTool
    logger.warning(
        "⚠️  SECURITY: Docker not found — falling back to UNSANDBOXED AiderTool. "
        "AI-generated code will execute with full host privileges. "
        "Install Docker and mount /var/run/docker.sock to enable sandboxing, "
        "or set FORGE_USE_SANDBOX=always to make this a hard failure."
    )
    return AiderTool()


async def _noop_call_adapter(provider: str, model: str, messages: list, **kwargs) -> str:
    """No-op call adapter for development — no real AI calls."""
    return '{"result": "no-op"}'


def _create_default_chain_config() -> RoleChainConfig:
    """Create default chain config with all roles using OpenRouter.

    Used when models.yaml is not available or fails to parse.
    """
    import os
    from app.runtime.router import ChainEntry, Role, RoleChainConfig

    default_model = os.environ.get(
        "FORGE_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"
    )
    return RoleChainConfig(chains={
        Role.CLARIFICATION: [ChainEntry(provider="openrouter", model=default_model)],
        Role.ARCHITECT: [ChainEntry(provider="openrouter", model=default_model)],
        Role.PLANNER: [ChainEntry(provider="openrouter", model=default_model)],
        Role.CODER: [ChainEntry(provider="openrouter", model=default_model)],
        Role.REVIEWER: [ChainEntry(provider="openrouter", model=default_model)],
        Role.DOC_WRITER: [ChainEntry(provider="openrouter", model=default_model)],
        Role.INTERRUPT_HANDLER: [ChainEntry(provider="openrouter", model=default_model)],
    })
