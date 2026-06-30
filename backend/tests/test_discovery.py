"""Tests for the Discovery bootstrap procedure.

Validates requirements 13.1-13.6, 15.3-15.5:
- Config loading and validation
- Schema validation error halts startup within 1 second
- Concurrent resource probing with per-probe timeout of 5 seconds
- Only healthy resources registered
- capability.degraded emitted for unhealthy resources
- forge.boot.discovery_complete emitted with healthy/unhealthy sets
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from app.runtime.discovery import (
    ConfigValidationError,
    DiscoveryResult,
    PROBE_TIMEOUT_SECONDS,
    ResourceConfig,
    extract_resources_from_config,
    load_and_validate_configs,
    probe_resources,
    run_discovery,
)
from app.runtime.events.models import EventType
from app.runtime.models import Capability, CapabilityKind, Role
from app.runtime.registry import CapabilityRegistry
from app.runtime.types import Health


# --- Fixtures ---


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with valid YAML files."""
    models = {
        "roles": {
            "coder": {
                "chain": [
                    {"provider": "openai", "model": "gpt-4o"},
                    {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
                ]
            },
            "reviewer": {
                "chain": [
                    {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
                ]
            },
        },
        "providers": {
            "openai": {"base_url": "https://api.openai.com", "api_key_env": "OPENAI_API_KEY"},
            "anthropic": {
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
            },
        },
    }
    policies = {
        "retry_budget": {"max_retries_per_task": 3},
        "escalation": {"failure_threshold": 2},
        "rules": [{"on": "verification_failure", "stage": "tests", "decision": "retry"}],
    }
    rate_limits = {
        "session": {"max_tokens": 1000000, "max_cost_usd": 50.0},
        "providers": {"openai": {"requests_per_minute": 60}},
    }
    tools = {
        "tools": {
            "aider": {"enabled": True, "command": "aider", "timeout_s": 300},
            "openhands": {"enabled": False, "command": "openhands"},
        }
    }
    verification = {
        "stages": {
            "advisory": [{"name": "lint", "timeout_s": 60, "command": "ruff check ."}],
            "blocking": [{"name": "tests", "timeout_s": 300, "command": "pytest"}],
        },
        "defaults": {"advisory_timeout_s": 60, "blocking_timeout_s": 300},
    }

    (tmp_path / "models.yaml").write_text(yaml.dump(models))
    (tmp_path / "policies.yaml").write_text(yaml.dump(policies))
    (tmp_path / "rate_limits.yaml").write_text(yaml.dump(rate_limits))
    (tmp_path / "tools.yaml").write_text(yaml.dump(tools))
    (tmp_path / "verification.yaml").write_text(yaml.dump(verification))

    return tmp_path


@pytest.fixture
def registry() -> CapabilityRegistry:
    """Create a fresh CapabilityRegistry for testing."""
    return CapabilityRegistry(session_id="boot")


def make_healthy_probe() -> AsyncMock:
    """Create a mock probe that returns healthy."""
    probe = AsyncMock()
    probe.health_check = AsyncMock(return_value=Health.healthy(latency_ms=10.0))
    return probe


def make_unhealthy_probe(message: str = "connection refused") -> AsyncMock:
    """Create a mock probe that returns unhealthy."""
    probe = AsyncMock()
    probe.health_check = AsyncMock(
        return_value=Health.unhealthy(message=message, latency_ms=50.0)
    )
    return probe


def make_timeout_probe(delay: float = 10.0) -> AsyncMock:
    """Create a mock probe that times out."""
    probe = AsyncMock()

    async def slow_check() -> Health:
        await asyncio.sleep(delay)
        return Health.healthy()

    probe.health_check = slow_check
    return probe


# --- Config Loading and Validation Tests ---


class TestConfigLoading:
    """Test configuration loading and validation (R13.1, R13.2)."""

    def test_load_valid_configs(self, config_dir: Path) -> None:
        """Valid configuration files are loaded and parsed correctly."""
        configs = load_and_validate_configs(config_dir)

        assert "models.yaml" in configs
        assert "policies.yaml" in configs
        assert "rate_limits.yaml" in configs
        assert "tools.yaml" in configs
        assert "verification.yaml" in configs

    def test_missing_config_file_raises(self, tmp_path: Path) -> None:
        """Missing configuration file raises ConfigValidationError."""
        # Empty directory — no config files
        with pytest.raises(ConfigValidationError) as exc_info:
            load_and_validate_configs(tmp_path)

        assert "not found" in exc_info.value.reason.lower() or "File not found" in str(
            exc_info.value
        )

    def test_invalid_yaml_raises(self, config_dir: Path) -> None:
        """Malformed YAML raises ConfigValidationError."""
        # Write truly invalid YAML (unclosed bracket triggers a parse error)
        (config_dir / "models.yaml").write_text("key: [unclosed\n  - bad")

        with pytest.raises(ConfigValidationError) as exc_info:
            load_and_validate_configs(config_dir)

        assert exc_info.value.file_name == "models.yaml"
        assert "parse error" in exc_info.value.reason.lower()

    def test_missing_required_key_raises(self, config_dir: Path) -> None:
        """Missing required key in config raises ConfigValidationError."""
        # Write models.yaml without 'roles' key
        (config_dir / "models.yaml").write_text(yaml.dump({"providers": {}}))

        with pytest.raises(ConfigValidationError) as exc_info:
            load_and_validate_configs(config_dir)

        assert exc_info.value.file_name == "models.yaml"
        assert "roles" in exc_info.value.reason

    def test_wrong_type_for_key_raises(self, config_dir: Path) -> None:
        """Wrong type for a required key raises ConfigValidationError."""
        # Write models.yaml with 'roles' as a string instead of dict
        (config_dir / "models.yaml").write_text(
            yaml.dump({"roles": "not a dict", "providers": {}})
        )

        with pytest.raises(ConfigValidationError) as exc_info:
            load_and_validate_configs(config_dir)

        assert "must be of type" in exc_info.value.reason

    def test_empty_file_raises(self, config_dir: Path) -> None:
        """Empty config file raises ConfigValidationError."""
        (config_dir / "models.yaml").write_text("")

        with pytest.raises(ConfigValidationError) as exc_info:
            load_and_validate_configs(config_dir)

        assert "empty" in exc_info.value.reason.lower()

    def test_validation_halts_within_1_second(self, config_dir: Path) -> None:
        """Config validation failure halts within 1 second (R13.2)."""
        (config_dir / "models.yaml").write_text(yaml.dump({"wrong": "schema"}))

        start = time.perf_counter()
        with pytest.raises(ConfigValidationError):
            load_and_validate_configs(config_dir)
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0, f"Validation took {elapsed:.3f}s, must be < 1.0s"


# --- Resource Extraction Tests ---


class TestResourceExtraction:
    """Test extracting resources from validated configuration."""

    def test_extract_ai_providers(self, config_dir: Path) -> None:
        """AI providers are extracted from models.yaml."""
        configs = load_and_validate_configs(config_dir)
        resources = extract_resources_from_config(configs)

        ai_resources = [r for r in resources if r.kind == CapabilityKind.AI_PROVIDER]
        assert len(ai_resources) >= 1

    def test_extract_coding_tools(self, config_dir: Path) -> None:
        """Enabled coding tools are extracted from tools.yaml."""
        configs = load_and_validate_configs(config_dir)
        resources = extract_resources_from_config(configs)

        tool_resources = [r for r in resources if r.kind == CapabilityKind.CODING_TOOL]
        # aider is enabled, openhands is not
        assert len(tool_resources) == 1
        assert tool_resources[0].name == Capability.TOOL_AIDER

    def test_disabled_tools_not_extracted(self, config_dir: Path) -> None:
        """Disabled tools are not extracted."""
        configs = load_and_validate_configs(config_dir)
        resources = extract_resources_from_config(configs)

        names = [r.name for r in resources]
        assert Capability.TOOL_OPENHANDS not in names

    def test_provider_roles_are_mapped(self, config_dir: Path) -> None:
        """Provider roles are extracted from role chain config."""
        configs = load_and_validate_configs(config_dir)
        resources = extract_resources_from_config(configs)

        openai_resource = next(
            (r for r in resources if r.provider_name == "openai"), None
        )
        assert openai_resource is not None
        assert Role.CODER in openai_resource.roles


# --- Concurrent Probing Tests ---


class TestProbing:
    """Test concurrent resource probing (R13.3, R13.4, R13.5)."""

    async def test_healthy_resource_returns_healthy_result(self) -> None:
        """Healthy resource probe returns a healthy ProbeResult."""
        resource = ResourceConfig(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            provider_name="openai",
        )
        probe = make_healthy_probe()

        result = await probe_resources(
            [resource], {Capability.AI_CODER: probe}
        )

        assert len(result.healthy) == 1
        assert len(result.unhealthy) == 0
        assert result.healthy[0].name == Capability.AI_CODER

    async def test_unhealthy_resource_returns_unhealthy_result(self) -> None:
        """Unhealthy resource probe returns an unhealthy ProbeResult."""
        resource = ResourceConfig(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            provider_name="openai",
        )
        probe = make_unhealthy_probe("connection refused")

        result = await probe_resources(
            [resource], {Capability.AI_CODER: probe}
        )

        assert len(result.healthy) == 0
        assert len(result.unhealthy) == 1
        assert result.unhealthy[0][0].name == Capability.AI_CODER
        assert "connection refused" in result.unhealthy[0][1]

    async def test_timeout_resource_marked_unhealthy(self) -> None:
        """Resource that times out is marked unhealthy (R13.5)."""
        resource = ResourceConfig(
            name=Capability.TOOL_AIDER,
            kind=CapabilityKind.CODING_TOOL,
            provider_name="aider",
        )
        probe = make_timeout_probe(delay=10.0)

        # Use a short timeout for testing
        result = await probe_resources(
            [resource], {Capability.TOOL_AIDER: probe}, timeout=0.1
        )

        assert len(result.healthy) == 0
        assert len(result.unhealthy) == 1
        assert "timed out" in result.unhealthy[0][1].lower()

    async def test_concurrent_probing(self) -> None:
        """Multiple resources are probed concurrently (R13.3)."""
        resources = [
            ResourceConfig(
                name=Capability.AI_CODER,
                kind=CapabilityKind.AI_PROVIDER,
                provider_name="openai",
            ),
            ResourceConfig(
                name=Capability.TOOL_AIDER,
                kind=CapabilityKind.CODING_TOOL,
                provider_name="aider",
            ),
        ]

        # Both probes take 0.2s — if sequential, total > 0.4s
        probe1 = AsyncMock()
        probe2 = AsyncMock()

        async def slow_healthy() -> Health:
            await asyncio.sleep(0.2)
            return Health.healthy(latency_ms=200.0)

        probe1.health_check = slow_healthy
        probe2.health_check = slow_healthy

        probe_map = {
            Capability.AI_CODER: probe1,
            Capability.TOOL_AIDER: probe2,
        }

        start = time.perf_counter()
        result = await probe_resources(resources, probe_map)
        elapsed = time.perf_counter() - start

        assert len(result.healthy) == 2
        # Concurrent: should take ~0.2s, not ~0.4s
        assert elapsed < 0.35, f"Probing took {elapsed:.3f}s, expected concurrent execution"

    async def test_missing_probe_marked_unhealthy(self) -> None:
        """Resource with no probe function is marked unhealthy."""
        resource = ResourceConfig(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            provider_name="openai",
        )

        result = await probe_resources([resource], {})  # No probe in map

        assert len(result.healthy) == 0
        assert len(result.unhealthy) == 1
        assert "no probe" in result.unhealthy[0][1].lower()

    async def test_probe_exception_marked_unhealthy(self) -> None:
        """Resource whose probe raises an exception is marked unhealthy."""
        resource = ResourceConfig(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            provider_name="openai",
        )
        probe = AsyncMock()
        probe.health_check = AsyncMock(side_effect=RuntimeError("network error"))

        result = await probe_resources(
            [resource], {Capability.AI_CODER: probe}
        )

        assert len(result.healthy) == 0
        assert len(result.unhealthy) == 1
        assert "network error" in result.unhealthy[0][1]


# --- Full Discovery Integration Tests ---


class TestRunDiscovery:
    """Test the full run_discovery procedure (R13.1-R13.6, R15.3-R15.5)."""

    async def test_healthy_resources_registered(
        self, config_dir: Path, registry: CapabilityRegistry
    ) -> None:
        """Healthy resources are registered in the CapabilityRegistry (R13.4)."""
        probe_map = {
            Capability.AI_CODER: make_healthy_probe(),
            Capability.AI_ARCHITECT: make_healthy_probe(),
            Capability.TOOL_AIDER: make_healthy_probe(),
        }

        result = await run_discovery(
            config_dir=config_dir,
            registry=registry,
            probe_map=probe_map,
        )

        # At least one healthy resource should be registered
        assert len(result.healthy) > 0
        for resource in result.healthy:
            assert registry.has(resource.name)

    async def test_unhealthy_resources_not_registered(
        self, config_dir: Path, registry: CapabilityRegistry
    ) -> None:
        """Unhealthy resources are NOT registered in the Registry (R13.5)."""
        probe_map = {
            Capability.AI_CODER: make_unhealthy_probe("down"),
            Capability.AI_ARCHITECT: make_unhealthy_probe("down"),
            Capability.TOOL_AIDER: make_unhealthy_probe("down"),
        }

        result = await run_discovery(
            config_dir=config_dir,
            registry=registry,
            probe_map=probe_map,
        )

        assert len(result.unhealthy) > 0
        for resource, reason in result.unhealthy:
            assert not registry.has(resource.name)

    async def test_degraded_event_emitted_for_unhealthy(
        self, config_dir: Path, registry: CapabilityRegistry
    ) -> None:
        """capability.degraded event emitted for unhealthy resources (R13.5)."""
        emitted_events: list = []
        event_emitter = AsyncMock(side_effect=lambda e: emitted_events.append(e))

        probe_map = {
            Capability.AI_CODER: make_unhealthy_probe("connection refused"),
            Capability.AI_ARCHITECT: make_healthy_probe(),
            Capability.TOOL_AIDER: make_healthy_probe(),
        }

        await run_discovery(
            config_dir=config_dir,
            registry=registry,
            probe_map=probe_map,
            event_emitter=event_emitter,
        )

        degraded_events = [
            e for e in emitted_events if e.type == EventType.CAPABILITY_DEGRADED
        ]
        assert len(degraded_events) >= 1
        # Verify payload contains reason
        for evt in degraded_events:
            assert "reason" in evt.payload
            assert "capability" in evt.payload

    async def test_discovery_complete_event_emitted(
        self, config_dir: Path, registry: CapabilityRegistry
    ) -> None:
        """forge.boot.discovery_complete event emitted (R13.6)."""
        emitted_events: list = []
        event_emitter = AsyncMock(side_effect=lambda e: emitted_events.append(e))

        probe_map = {
            Capability.AI_CODER: make_healthy_probe(),
            Capability.AI_ARCHITECT: make_healthy_probe(),
            Capability.TOOL_AIDER: make_unhealthy_probe("down"),
        }

        await run_discovery(
            config_dir=config_dir,
            registry=registry,
            probe_map=probe_map,
            event_emitter=event_emitter,
        )

        complete_events = [
            e
            for e in emitted_events
            if e.type == EventType.FORGE_BOOT_DISCOVERY_COMPLETE
        ]
        assert len(complete_events) == 1
        payload = complete_events[0].payload
        assert "healthy" in payload
        assert "unhealthy" in payload
        assert isinstance(payload["healthy"], list)
        assert isinstance(payload["unhealthy"], list)

    async def test_config_validation_error_halts_startup(
        self, tmp_path: Path, registry: CapabilityRegistry
    ) -> None:
        """ConfigValidationError halts startup on bad config (R13.2)."""
        # Create an empty config dir (missing files)
        start = time.perf_counter()
        with pytest.raises(ConfigValidationError):
            await run_discovery(
                config_dir=tmp_path,
                registry=registry,
                probe_map={},
            )
        elapsed = time.perf_counter() - start

        # Must halt within 1 second
        assert elapsed < 1.0
        # Registry must remain empty
        assert len(registry.entries) == 0

    async def test_no_partial_registration_on_config_error(
        self, tmp_path: Path, registry: CapabilityRegistry
    ) -> None:
        """No capabilities registered when config validation fails (R13.2)."""
        with pytest.raises(ConfigValidationError):
            await run_discovery(
                config_dir=tmp_path,
                registry=registry,
                probe_map={},
            )

        assert len(registry.entries) == 0

    async def test_discovery_with_mixed_health(
        self, config_dir: Path, registry: CapabilityRegistry
    ) -> None:
        """Mix of healthy and unhealthy resources produces correct sets."""
        probe_map = {
            Capability.AI_CODER: make_healthy_probe(),
            Capability.AI_ARCHITECT: make_unhealthy_probe("timeout"),
            Capability.TOOL_AIDER: make_healthy_probe(),
        }

        result = await run_discovery(
            config_dir=config_dir,
            registry=registry,
            probe_map=probe_map,
        )

        # Verify healthy registered, unhealthy not
        healthy_names = [r.name for r in result.healthy]
        unhealthy_names = [r.name for r, _ in result.unhealthy]

        for name in healthy_names:
            assert registry.has(name)
        for name in unhealthy_names:
            assert not registry.has(name)

    async def test_discovery_complete_payload_has_both_sets(
        self, config_dir: Path, registry: CapabilityRegistry
    ) -> None:
        """discovery_complete payload contains both healthy and unhealthy lists."""
        emitted_events: list = []
        event_emitter = AsyncMock(side_effect=lambda e: emitted_events.append(e))

        probe_map = {
            Capability.AI_CODER: make_healthy_probe(),
            Capability.AI_ARCHITECT: make_unhealthy_probe("refused"),
            Capability.TOOL_AIDER: make_healthy_probe(),
        }

        await run_discovery(
            config_dir=config_dir,
            registry=registry,
            probe_map=probe_map,
            event_emitter=event_emitter,
        )

        complete_events = [
            e
            for e in emitted_events
            if e.type == EventType.FORGE_BOOT_DISCOVERY_COMPLETE
        ]
        assert len(complete_events) == 1
        payload = complete_events[0].payload

        # Healthy list should contain registered resources
        assert len(payload["healthy"]) > 0
        # Unhealthy list should contain failed resources with reasons
        assert len(payload["unhealthy"]) > 0
        for entry in payload["unhealthy"]:
            assert "capability" in entry
            assert "reason" in entry

    async def test_discovery_complete_payload_includes_duration_and_total(
        self, config_dir: Path, registry: CapabilityRegistry
    ) -> None:
        """discovery_complete payload includes duration_ms and total_resources (R13.6)."""
        emitted_events: list = []
        event_emitter = AsyncMock(side_effect=lambda e: emitted_events.append(e))

        probe_map = {
            Capability.AI_CODER: make_healthy_probe(),
            Capability.AI_ARCHITECT: make_healthy_probe(),
            Capability.TOOL_AIDER: make_healthy_probe(),
        }

        await run_discovery(
            config_dir=config_dir,
            registry=registry,
            probe_map=probe_map,
            event_emitter=event_emitter,
        )

        complete_events = [
            e
            for e in emitted_events
            if e.type == EventType.FORGE_BOOT_DISCOVERY_COMPLETE
        ]
        assert len(complete_events) == 1
        payload = complete_events[0].payload

        assert "duration_ms" in payload
        assert isinstance(payload["duration_ms"], float)
        assert payload["duration_ms"] >= 0

        assert "total_resources" in payload
        assert isinstance(payload["total_resources"], int)
        assert payload["total_resources"] > 0

    async def test_probe_timeout_default_is_5_seconds(self) -> None:
        """Per-probe timeout default is 5 seconds (R13.3)."""
        assert PROBE_TIMEOUT_SECONDS == 5.0
