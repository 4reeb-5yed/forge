"""Discovery bootstrap procedure — probes configured resources at startup.

The Discovery procedure is a one-shot bootstrap activity:
1. Load and validate all configuration YAML files.
2. Halt startup within 1 second on any schema validation error.
3. Probe all configured resources concurrently (per-probe timeout: 5s).
4. Register only healthy resources in the CapabilityRegistry.
5. Emit capability.degraded for unhealthy or timed-out resources.
6. Emit forge.boot.discovery_complete with healthy/unhealthy sets.

Per design R7: Discovery writes to the Registry once at boot. Only Health Monitor
updates it continuously afterward.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from app.runtime.events.models import Event, EventType
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    Role,
)
from app.runtime.registry import CapabilityRegistry
from app.runtime.types import Health

logger = logging.getLogger(__name__)

# Per-probe timeout in seconds (requirement 13.3)
PROBE_TIMEOUT_SECONDS = 5.0

# Maximum time allowed for config validation failure to halt startup (requirement 13.2)
VALIDATION_HALT_TIMEOUT_SECONDS = 1.0


class ConfigValidationError(Exception):
    """Raised when a configuration file fails schema validation.

    Per requirement 13.2: halt startup within 1 second on schema errors.
    """

    def __init__(self, file_name: str, reason: str) -> None:
        self.file_name = file_name
        self.reason = reason
        super().__init__(f"Config validation failed for '{file_name}': {reason}")


@runtime_checkable
class Probeable(Protocol):
    """Protocol for resources that can be health-checked during discovery."""

    async def health_check(self) -> Health: ...


@dataclass(frozen=True)
class ResourceConfig:
    """Configuration for a single discoverable resource."""

    name: Capability
    kind: CapabilityKind
    roles: list[Role] = field(default_factory=list)
    provider_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    """Result of probing a single resource."""

    resource: ResourceConfig
    healthy: bool
    reason: str = ""
    latency_ms: float | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """Complete discovery result with healthy and unhealthy resource sets."""

    healthy: list[ResourceConfig] = field(default_factory=list)
    unhealthy: list[tuple[ResourceConfig, str]] = field(default_factory=list)
    duration_ms: float = 0.0


# --- Configuration Schema Definitions ---

# Required top-level keys per config file
CONFIG_SCHEMAS: dict[str, dict[str, type]] = {
    "models.yaml": {"roles": dict, "providers": dict},
    "policies.yaml": {"retry_budget": dict, "rules": list},
    "rate_limits.yaml": {"session": dict},
    "tools.yaml": {"tools": dict},
    "verification.yaml": {"stages": dict},
}


def _validate_config_file(file_name: str, data: Any) -> None:
    """Validate a loaded config file against its expected schema.

    Args:
        file_name: Name of the config file.
        data: Parsed YAML content.

    Raises:
        ConfigValidationError: If validation fails.
    """
    if data is None:
        raise ConfigValidationError(file_name, "File is empty or contains no YAML content")

    if not isinstance(data, dict):
        raise ConfigValidationError(file_name, "Top-level structure must be a mapping")

    schema = CONFIG_SCHEMAS.get(file_name)
    if schema is None:
        # Unknown config file — no schema to validate against
        return

    for key, expected_type in schema.items():
        if key not in data:
            raise ConfigValidationError(
                file_name, f"Missing required key: '{key}'"
            )
        if not isinstance(data[key], expected_type):
            raise ConfigValidationError(
                file_name,
                f"Key '{key}' must be of type {expected_type.__name__}, "
                f"got {type(data[key]).__name__}",
            )


def load_and_validate_configs(
    config_dir: Path | str,
) -> dict[str, Any]:
    """Load and validate all configuration YAML files.

    Per requirement 13.1: load and validate config before probing any resource.
    Per requirement 13.2: halt (raise) within 1 second on schema validation errors.

    Args:
        config_dir: Path to the directory containing YAML config files.

    Returns:
        Dictionary mapping config file names to their parsed content.

    Raises:
        ConfigValidationError: If any config file is missing or invalid.
    """
    config_dir = Path(config_dir)
    configs: dict[str, Any] = {}

    required_files = list(CONFIG_SCHEMAS.keys())

    for file_name in required_files:
        file_path = config_dir / file_name
        if not file_path.exists():
            raise ConfigValidationError(file_name, f"File not found: {file_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigValidationError(file_name, f"YAML parse error: {e}") from e

        _validate_config_file(file_name, data)
        configs[file_name] = data

    return configs


def extract_resources_from_config(configs: dict[str, Any]) -> list[ResourceConfig]:
    """Extract discoverable resources from validated configuration.

    Maps configuration entries to ResourceConfig objects describing
    what should be probed during discovery.

    Args:
        configs: Dictionary of validated config file data.

    Returns:
        List of ResourceConfig objects to probe.
    """
    resources: list[ResourceConfig] = []

    # Extract AI providers from models.yaml
    models_config = configs.get("models.yaml", {})
    providers = models_config.get("providers", {})

    # Map provider names to capabilities
    provider_capability_map: dict[str, Capability] = {
        "openai": Capability.AI_CODER,  # Primary provider
        "anthropic": Capability.AI_ARCHITECT,  # Secondary provider
    }

    # Determine roles served by each provider from the chain config
    roles_config = models_config.get("roles", {})
    provider_roles: dict[str, list[Role]] = {}

    role_name_map: dict[str, Role] = {
        "clarification": Role.CLARIFICATION,
        "architect": Role.ARCHITECT,
        "planner": Role.PLANNER,
        "coder": Role.CODER,
        "reviewer": Role.REVIEWER,
        "doc_writer": Role.DOC_WRITER,
        "interrupt_handler": Role.INTERRUPT_HANDLER,
    }

    for role_name, role_config in roles_config.items():
        if role_name in role_name_map:
            chain = role_config.get("chain", [])
            for entry in chain:
                prov_name = entry.get("provider", "")
                if prov_name not in provider_roles:
                    provider_roles[prov_name] = []
                role = role_name_map[role_name]
                if role not in provider_roles[prov_name]:
                    provider_roles[prov_name].append(role)

    for provider_name, provider_config in providers.items():
        capability = provider_capability_map.get(provider_name)
        if capability is None:
            # Generic AI provider capability name
            continue

        roles = provider_roles.get(provider_name, [])
        resources.append(
            ResourceConfig(
                name=capability,
                kind=CapabilityKind.AI_PROVIDER,
                roles=roles,
                provider_name=provider_name,
                metadata={"config": provider_config},
            )
        )

    # Extract coding tools from tools.yaml
    tools_config = configs.get("tools.yaml", {})
    tools = tools_config.get("tools", {})

    tool_capability_map: dict[str, Capability] = {
        "aider": Capability.TOOL_AIDER,
        "openhands": Capability.TOOL_OPENHANDS,
    }

    for tool_name, tool_config in tools.items():
        if not tool_config.get("enabled", False):
            continue
        capability = tool_capability_map.get(tool_name)
        if capability is None:
            continue
        resources.append(
            ResourceConfig(
                name=capability,
                kind=CapabilityKind.CODING_TOOL,
                roles=[],
                provider_name=tool_name,
                metadata={"config": tool_config},
            )
        )

    return resources


async def _probe_resource(
    resource: ResourceConfig,
    probe_fn: Probeable | None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Probe a single resource with a timeout.

    Per requirement 13.3: per-probe timeout of 5 seconds.

    Args:
        resource: The resource configuration to probe.
        probe_fn: Object implementing the Probeable protocol (health_check).
        timeout: Maximum time to wait for the probe (seconds).

    Returns:
        ProbeResult indicating whether the resource is healthy.
    """
    if probe_fn is None:
        return ProbeResult(
            resource=resource,
            healthy=False,
            reason="No probe function available for resource",
        )

    try:
        health = await asyncio.wait_for(
            probe_fn.health_check(),
            timeout=timeout,
        )
        if health.ok:
            return ProbeResult(
                resource=resource,
                healthy=True,
                latency_ms=health.latency_ms,
            )
        else:
            return ProbeResult(
                resource=resource,
                healthy=False,
                reason=health.message or f"Health check returned unhealthy: {health.status.value}",
                latency_ms=health.latency_ms,
            )
    except asyncio.TimeoutError:
        return ProbeResult(
            resource=resource,
            healthy=False,
            reason=f"Health check timed out after {timeout}s",
        )
    except Exception as e:
        return ProbeResult(
            resource=resource,
            healthy=False,
            reason=f"Health check failed with error: {e}",
        )


async def probe_resources(
    resources: list[ResourceConfig],
    probe_map: dict[Capability, Probeable],
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> DiscoveryResult:
    """Probe all configured resources concurrently.

    Per requirement 13.3: probe all configured resources concurrently using
    a per-probe timeout of 5 seconds.

    Args:
        resources: List of resources to probe.
        probe_map: Mapping of capability names to their probe implementations.
        timeout: Per-probe timeout in seconds.

    Returns:
        DiscoveryResult with healthy and unhealthy resource sets.
    """
    start_time = time.perf_counter()

    tasks = [
        _probe_resource(resource, probe_map.get(resource.name), timeout)
        for resource in resources
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    healthy: list[ResourceConfig] = []
    unhealthy: list[tuple[ResourceConfig, str]] = []

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            unhealthy.append((resources[i], f"Probe raised exception: {result}"))
        elif result.healthy:
            healthy.append(result.resource)
        else:
            unhealthy.append((result.resource, result.reason))

    duration_ms = (time.perf_counter() - start_time) * 1000

    return DiscoveryResult(
        healthy=healthy,
        unhealthy=unhealthy,
        duration_ms=duration_ms,
    )


async def run_discovery(
    config_dir: Path | str,
    registry: CapabilityRegistry,
    probe_map: dict[Capability, Probeable],
    event_emitter: Any | None = None,
    session_id: str = "boot",
) -> DiscoveryResult:
    """Execute the full Discovery bootstrap procedure.

    Orchestrates:
    1. Load and validate configuration YAML files
    2. Extract resource configs
    3. Probe all resources concurrently
    4. Register healthy resources in the CapabilityRegistry
    5. Emit capability.degraded for unhealthy resources
    6. Emit forge.boot.discovery_complete

    Args:
        config_dir: Path to configuration directory.
        registry: The CapabilityRegistry to populate.
        probe_map: Mapping of capability names to probe implementations.
        event_emitter: Optional async callable for event emission (e.g., EventBus.publish).
        session_id: Session ID for events (typically "boot").

    Returns:
        DiscoveryResult with healthy/unhealthy sets.

    Raises:
        ConfigValidationError: If config validation fails (halts startup).
    """
    # Step 1: Load and validate configs (requirement 13.1, 13.2)
    configs = load_and_validate_configs(config_dir)
    logger.info("Configuration loaded and validated successfully")

    # Step 2: Extract resources from config
    resources = extract_resources_from_config(configs)
    logger.info("Extracted %d resources from configuration", len(resources))

    # Step 3: Probe all resources concurrently (requirement 13.3)
    discovery_result = await probe_resources(resources, probe_map)

    # Step 4: Register healthy resources (requirement 13.4)
    for resource in discovery_result.healthy:
        entry = CapabilityEntry(
            name=resource.name,
            kind=resource.kind,
            healthy=True,
            roles=resource.roles,
            provider_name=resource.provider_name,
            metadata=resource.metadata,
        )
        await registry.register(entry)
        logger.info("Registered healthy capability: %s", resource.name.value)

    # Step 5: Emit capability.degraded for unhealthy resources (requirement 13.5)
    for resource, reason in discovery_result.unhealthy:
        if event_emitter is not None:
            degraded_event = Event.create(
                type=EventType.CAPABILITY_DEGRADED,
                session_id=session_id,
                source="discovery",
                payload={
                    "capability": resource.name.value,
                    "kind": resource.kind.value,
                    "reason": reason,
                    "provider_name": resource.provider_name,
                },
                correlation_id=session_id,
                event_id=f"discovery_degraded_{resource.name.value}",
            )
            await event_emitter(degraded_event)
        logger.warning(
            "Resource unhealthy during discovery: %s — %s",
            resource.name.value,
            reason,
        )

    # Step 6: Emit forge.boot.discovery_complete (requirement 13.6)
    if event_emitter is not None:
        complete_event = Event.create(
            type=EventType.FORGE_BOOT_DISCOVERY_COMPLETE,
            session_id=session_id,
            source="discovery",
            payload={
                "healthy": [r.name.value for r in discovery_result.healthy],
                "unhealthy": [
                    {"capability": r.name.value, "reason": reason}
                    for r, reason in discovery_result.unhealthy
                ],
                "duration_ms": discovery_result.duration_ms,
                "total_resources": len(resources),
            },
            correlation_id=session_id,
            event_id="forge_boot_discovery_complete",
        )
        await event_emitter(complete_event)

    logger.info(
        "Discovery complete: %d healthy, %d unhealthy (%.1fms)",
        len(discovery_result.healthy),
        len(discovery_result.unhealthy),
        discovery_result.duration_ms,
    )

    return discovery_result
