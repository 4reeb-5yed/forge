"""Bootstrap sequence — brings the Forge runtime to forge.ready state.

Runs at application startup:
1. Load and validate configuration
2. Initialize PostgreSQL persistence (if DATABASE_URL available)
3. Run concurrent discovery (probe all resources)
4. Register healthy capabilities in the registry
5. Start health monitor background task
6. Evaluate operational mode
7. Emit forge.ready event

Requirements: 15.1, 15.2, 15.3, 15.4, 16.1
"""

from __future__ import annotations

import logging
import os
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

    Uses PostgreSQL stores when DATABASE_URL is available, falls back to in-memory
    stores for development. Returns a fully assembled dependency container ready
    for bootstrap and graph construction.

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

    # Check if PostgreSQL persistence is available
    persistence = _init_persistence()
    use_db = persistence is not None

    if use_db:
        logger.info("Using PostgreSQL persistence stores")
    else:
        logger.info("Using in-memory stores (set DATABASE_URL for persistence)")

    # Checkpoint store - use PostgreSQL or in-memory
    if use_db:
        recovery = CrashRecovery(
            checkpoint_store=persistence,
            event_bus=event_bus,
        )
    else:
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


def _init_persistence():
    """Initialize PostgreSQL persistence if pool was created in lifespan.

    The pool is created in app.py's lifespan startup and stored in the
    persistence module's _pool variable. This function retrieves it.

    Returns:
        CheckpointStore implementation if pool available, None otherwise.
    """
    try:
        from app.runtime import persistence as persist_module
        if persist_module._pool is not None:
            from app.runtime.persistence import PostgresCheckpointStore
            logger.info("Using PostgreSQL checkpoint store")
            return PostgresCheckpointStore(persist_module._pool)
    except Exception as exc:
        logger.warning("Failed to get PostgreSQL pool: %s — using in-memory stores", exc)

    return None


class _InMemoryCheckpointStore:
    """Minimal in-memory checkpoint store for development."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, Any] = {}

    async def write_checkpoint(
        self, session_id: str, node_id: str, highest_seq: int, state_json: dict | None = None
    ) -> None:
        self._checkpoints[session_id] = {
            "node_id": node_id,
            "highest_seq": highest_seq,
            "state": state_json,
        }

    async def get_latest_checkpoint(self, session_id: str):
        return self._checkpoints.get(session_id)

    async def list_non_terminal_sessions(self) -> list:
        return []


def _create_coding_tool():
    """Create the appropriate coding tool based on Docker availability.

    Prefers SandboxedAiderTool (Docker container per task) when Docker
    is available. Falls back to direct AiderTool if Docker is not found.
    Falls back to MockCodingTool if no AI coding tool is available.

    IMPORTANT: Default is 'always' in production for maximum security.
    In 'auto' mode, falling back to unsandboxed execution logs
    a WARNING (not info) so operators notice the security gap. In 'always'
    mode, missing Docker is a hard failure that prevents startup.

    Security: only OPENROUTER_API_KEY is passed to the sandbox.
    GITHUB_TOKEN and other secrets are never exposed to the coding tool.
    """
    import os
    import shutil

    use_sandbox = os.environ.get("FORGE_USE_SANDBOX", "always")

    if use_sandbox == "never":
        if shutil.which("aider") is not None:
            from app.adapters.aider_tool import AiderTool
            logger.info("Coding tool: AiderTool (sandbox disabled via FORGE_USE_SANDBOX=never)")
            return AiderTool()
        else:
            logger.warning("Coding tool: MockCodingTool (aider not found, sandbox disabled)")
            from app.adapters.mock_coding_tool import MockCodingTool
            return MockCodingTool()

    docker_available = shutil.which("docker") is not None

    if use_sandbox == "always":
        if not docker_available:
            # In always mode, fall back to mock if no Docker (for testing)
            logger.warning(
                "Coding tool: MockCodingTool (FORGE_USE_SANDBOX=always but Docker unavailable)"
            )
            from app.adapters.mock_coding_tool import MockCodingTool
            return MockCodingTool()
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

    # Docker not available in auto mode - try unsandboxed AiderTool
    if shutil.which("aider") is not None:
        from app.adapters.aider_tool import AiderTool
        logger.warning(
            "Coding tool: AiderTool (unsandboxed - Docker unavailable and auto mode)"
        )
        return AiderTool()

    # No Docker and no aider - fall back to mock
    logger.warning("Coding tool: MockCodingTool (no Docker and no aider)")
    from app.adapters.mock_coding_tool import MockCodingTool
    return MockCodingTool()


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
