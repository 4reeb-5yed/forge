"""Property-based tests for discovery soundness using Hypothesis.

**Validates: Requirements 13.4, 13.5, 15.2**

Property 2: Discovery soundness — for every capability in the registry after
    bootstrap, its last health_check returned ok=true; no unhealthy capability
    is ever registered.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st, assume

from app.runtime.discovery import (
    DiscoveryResult,
    ProbeResult,
    ResourceConfig,
    probe_resources,
    run_discovery,
)
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    Role,
)
from app.runtime.registry import CapabilityRegistry
from app.runtime.types import Health


# ---------------------------------------------------------------------------
# Health Outcome Enum for generation
# ---------------------------------------------------------------------------


class HealthOutcome(str, Enum):
    """Possible outcomes for a resource health check during discovery."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# Fake Probe Implementation
# ---------------------------------------------------------------------------


class FakeDiscoveryProbe:
    """A configurable fake probe that returns a predetermined health outcome.

    Supports healthy, unhealthy, and timeout scenarios for property testing.
    """

    def __init__(self, outcome: HealthOutcome, *, latency_ms: float = 5.0) -> None:
        self.outcome = outcome
        self.latency_ms = latency_ms
        self.call_count = 0
        self.last_result: Health | None = None

    async def health_check(self) -> Health:
        self.call_count += 1
        if self.outcome == HealthOutcome.HEALTHY:
            result = Health.healthy(latency_ms=self.latency_ms)
            self.last_result = result
            return result
        elif self.outcome == HealthOutcome.UNHEALTHY:
            result = Health.unhealthy("resource is unhealthy", latency_ms=self.latency_ms)
            self.last_result = result
            return result
        elif self.outcome == HealthOutcome.TIMEOUT:
            # Simulate timeout by sleeping longer than any reasonable probe timeout
            await asyncio.sleep(100.0)
            # This line should never be reached due to asyncio.wait_for timeout
            result = Health.healthy(latency_ms=self.latency_ms)
            self.last_result = result
            return result
        raise ValueError(f"Unknown outcome: {self.outcome}")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# All capabilities that can appear in discovery
discoverable_capabilities = st.sampled_from([
    Capability.AI_CODER,
    Capability.AI_ARCHITECT,
    Capability.AI_REVIEWER,
    Capability.AI_DOC_WRITER,
    Capability.TOOL_AIDER,
    Capability.TOOL_OPENHANDS,
    Capability.VCS_GITHUB,
    Capability.STORE_R2,
    Capability.VECTOR_CHROMA,
])

# Capability kinds mapped from capabilities
CAPABILITY_KIND_MAP: dict[Capability, CapabilityKind] = {
    Capability.AI_CODER: CapabilityKind.AI_PROVIDER,
    Capability.AI_ARCHITECT: CapabilityKind.AI_PROVIDER,
    Capability.AI_REVIEWER: CapabilityKind.AI_PROVIDER,
    Capability.AI_DOC_WRITER: CapabilityKind.AI_PROVIDER,
    Capability.TOOL_AIDER: CapabilityKind.CODING_TOOL,
    Capability.TOOL_OPENHANDS: CapabilityKind.CODING_TOOL,
    Capability.VCS_GITHUB: CapabilityKind.VCS_CONNECTOR,
    Capability.STORE_R2: CapabilityKind.ARTIFACT_STORE,
    Capability.VECTOR_CHROMA: CapabilityKind.VECTOR_STORE,
}

# Health outcomes
health_outcomes = st.sampled_from(list(HealthOutcome))


@st.composite
def resource_with_outcome(draw: st.DrawFn) -> tuple[ResourceConfig, HealthOutcome]:
    """Generate a ResourceConfig paired with a random health outcome."""
    capability = draw(discoverable_capabilities)
    outcome = draw(health_outcomes)
    kind = CAPABILITY_KIND_MAP[capability]

    resource = ResourceConfig(
        name=capability,
        kind=kind,
        roles=[],
        provider_name=f"provider-{capability.value}",
        metadata={},
    )
    return resource, outcome


@st.composite
def resource_set_with_outcomes(
    draw: st.DrawFn,
) -> list[tuple[ResourceConfig, HealthOutcome]]:
    """Generate a set of unique resources each with a random health outcome.

    Ensures no duplicate capabilities (each capability appears at most once),
    generating between 1 and 9 resources (the full set of discoverable capabilities).
    """
    # Draw a subset of capabilities (unique)
    all_caps = list(CAPABILITY_KIND_MAP.keys())
    subset_size = draw(st.integers(min_value=1, max_value=len(all_caps)))
    chosen_caps = draw(
        st.lists(
            st.sampled_from(all_caps),
            min_size=subset_size,
            max_size=subset_size,
            unique=True,
        )
    )

    results: list[tuple[ResourceConfig, HealthOutcome]] = []
    for cap in chosen_caps:
        outcome = draw(health_outcomes)
        kind = CAPABILITY_KIND_MAP[cap]
        resource = ResourceConfig(
            name=cap,
            kind=kind,
            roles=[],
            provider_name=f"provider-{cap.value}",
            metadata={},
        )
        results.append((resource, outcome))

    return results


# ---------------------------------------------------------------------------
# Property 2: Discovery Soundness
# ---------------------------------------------------------------------------


class TestDiscoverySoundnessProperty:
    """Property 2: Discovery soundness — for every capability in the registry
    after bootstrap, its last health_check returned ok=true; no unhealthy
    capability is ever registered.

    **Validates: Requirements 13.4, 13.5, 15.2**
    """

    @given(resource_outcomes=resource_set_with_outcomes())
    @settings(max_examples=100, deadline=10000)
    async def test_only_healthy_resources_registered_after_discovery(
        self,
        resource_outcomes: list[tuple[ResourceConfig, HealthOutcome]],
    ) -> None:
        """For arbitrary sets of resources with random health check outcomes
        (healthy/unhealthy/timeout), after running discovery (probe_resources +
        registration), verify:
        1. Every capability in the registry had its probe return ok=true
        2. No unhealthy or timed-out capability appears in the registry

        Req 13.4: Only healthy resources are registered.
        Req 13.5: Unhealthy/timed-out resources are NOT registered.
        Req 15.2: Requesting unregistered capability raises error.
        """
        # Set up registry
        registry = CapabilityRegistry(session_id=f"test-{uuid.uuid4().hex[:8]}")

        # Build resources list and probe map from generated data
        resources: list[ResourceConfig] = []
        probe_map: dict[Capability, FakeDiscoveryProbe] = {}
        expected_healthy: set[Capability] = set()
        expected_unhealthy: set[Capability] = set()

        for resource, outcome in resource_outcomes:
            resources.append(resource)
            probe = FakeDiscoveryProbe(outcome)
            probe_map[resource.name] = probe

            if outcome == HealthOutcome.HEALTHY:
                expected_healthy.add(resource.name)
            else:
                expected_unhealthy.add(resource.name)

        # Run the probe phase (with a short timeout to trigger timeouts quickly)
        discovery_result = await probe_resources(
            resources, probe_map, timeout=0.05  # Very short timeout for test speed
        )

        # Register only healthy resources (mirrors run_discovery logic)
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

        # --- PROPERTY ASSERTIONS ---

        # 1. Every capability in the registry must have had a healthy probe
        for cap, entry in registry.entries.items():
            assert cap in expected_healthy, (
                f"Capability '{cap.value}' is in the registry but was expected "
                f"to be unhealthy/timed-out. Registry should only contain "
                f"resources whose last health_check returned ok=true."
            )
            # Verify the entry is marked healthy
            assert entry.healthy is True, (
                f"Capability '{cap.value}' is in the registry but marked "
                f"as unhealthy (entry.healthy=False)."
            )

        # 2. No unhealthy or timed-out capability is registered
        for cap in expected_unhealthy:
            assert not registry.has(cap), (
                f"Unhealthy/timed-out capability '{cap.value}' was registered "
                f"in the registry. Req 13.5: unhealthy resources SHALL NOT "
                f"be registered."
            )

        # 3. All expected healthy resources ARE in the registry
        for cap in expected_healthy:
            assert registry.has(cap), (
                f"Healthy capability '{cap.value}' was NOT registered. "
                f"Req 13.4: healthy resources SHALL be registered."
            )

    @given(resource_outcomes=resource_set_with_outcomes())
    @settings(max_examples=100, deadline=10000)
    async def test_unhealthy_resources_appear_in_discovery_unhealthy_set(
        self,
        resource_outcomes: list[tuple[ResourceConfig, HealthOutcome]],
    ) -> None:
        """For arbitrary sets of resources, all unhealthy/timed-out resources
        must appear in the discovery result's unhealthy set.

        Req 13.5: unhealthy or timed-out resources emit capability.degraded
        and are NOT registered. They appear in the unhealthy set of the
        discovery_complete event.
        """
        resources: list[ResourceConfig] = []
        probe_map: dict[Capability, FakeDiscoveryProbe] = {}
        expected_unhealthy: set[Capability] = set()

        for resource, outcome in resource_outcomes:
            resources.append(resource)
            probe = FakeDiscoveryProbe(outcome)
            probe_map[resource.name] = probe

            if outcome != HealthOutcome.HEALTHY:
                expected_unhealthy.add(resource.name)

        # Run probe phase
        discovery_result = await probe_resources(
            resources, probe_map, timeout=0.05
        )

        # Extract unhealthy capability names from the result
        unhealthy_caps = {r.name for r, _reason in discovery_result.unhealthy}

        # Every expected-unhealthy resource must be in the unhealthy set
        for cap in expected_unhealthy:
            assert cap in unhealthy_caps, (
                f"Expected unhealthy capability '{cap.value}' was not in "
                f"the discovery unhealthy set. Req 13.5: unhealthy resources "
                f"SHALL appear in the unhealthy results."
            )

        # No healthy resource should appear in the unhealthy set
        healthy_caps = {r.name for r in discovery_result.healthy}
        for cap in healthy_caps:
            assert cap not in unhealthy_caps, (
                f"Healthy capability '{cap.value}' appeared in both healthy "
                f"and unhealthy sets — contradictory."
            )

    @given(resource_outcomes=resource_set_with_outcomes())
    @settings(max_examples=100, deadline=10000)
    async def test_registry_and_discovery_result_are_consistent(
        self,
        resource_outcomes: list[tuple[ResourceConfig, HealthOutcome]],
    ) -> None:
        """The set of capabilities registered in the registry must exactly
        match the healthy set from discovery. No more, no less.

        This verifies the soundness invariant from both directions:
        registry ⊆ healthy_probed AND healthy_probed ⊆ registry.
        """
        registry = CapabilityRegistry(session_id=f"test-{uuid.uuid4().hex[:8]}")

        resources: list[ResourceConfig] = []
        probe_map: dict[Capability, FakeDiscoveryProbe] = {}

        for resource, outcome in resource_outcomes:
            resources.append(resource)
            probe = FakeDiscoveryProbe(outcome)
            probe_map[resource.name] = probe

        # Run probe phase
        discovery_result = await probe_resources(
            resources, probe_map, timeout=0.05
        )

        # Register healthy (same logic as run_discovery)
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

        # Registry capabilities must exactly equal the healthy discovery set
        registered_caps = set(registry.entries.keys())
        healthy_caps = {r.name for r in discovery_result.healthy}

        assert registered_caps == healthy_caps, (
            f"Registry contents do not match discovery healthy set. "
            f"Registry: {sorted(c.value for c in registered_caps)}, "
            f"Healthy: {sorted(c.value for c in healthy_caps)}"
        )
