"""Unit tests for the CapabilityRegistry.

Tests register/deregister/resolve operations, event emission,
lock-guarded mutations, and error handling.
"""

from __future__ import annotations

import asyncio

import pytest

from app.runtime.events.models import Event, EventType
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    Role,
)
from app.runtime.registry import CapabilityRegistry, CapabilityUnavailableError


# --- Fixtures ---


@pytest.fixture
def emitted_events() -> list[Event]:
    """Shared list to collect events emitted by the registry."""
    return []


@pytest.fixture
def event_emitter(emitted_events: list[Event]):
    """A mock event emitter that appends to the emitted_events list."""

    async def _emitter(event: Event) -> Event:
        emitted_events.append(event)
        return event

    return _emitter


@pytest.fixture
def registry(event_emitter) -> CapabilityRegistry:
    """A registry with a mock event emitter."""
    return CapabilityRegistry(event_emitter=event_emitter, session_id="test-session")


@pytest.fixture
def sample_entry() -> CapabilityEntry:
    """A sample capability entry for testing."""
    return CapabilityEntry(
        name=Capability.AI_CODER,
        kind=CapabilityKind.AI_PROVIDER,
        healthy=True,
        roles=[Role.CODER],
        provider_name="openrouter",
    )


@pytest.fixture
def vcs_entry() -> CapabilityEntry:
    """A VCS connector entry for testing."""
    return CapabilityEntry(
        name=Capability.VCS_GITHUB,
        kind=CapabilityKind.VCS_CONNECTOR,
        healthy=True,
        roles=[],
        provider_name="github",
    )


@pytest.fixture
def tool_entry() -> CapabilityEntry:
    """A coding tool entry for testing."""
    return CapabilityEntry(
        name=Capability.TOOL_AIDER,
        kind=CapabilityKind.CODING_TOOL,
        healthy=True,
        roles=[],
        provider_name="aider",
    )


# --- Test register() ---


class TestRegister:
    async def test_register_adds_capability(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        assert registry.has(Capability.AI_CODER)

    async def test_register_emits_event(
        self,
        registry: CapabilityRegistry,
        sample_entry: CapabilityEntry,
        emitted_events: list[Event],
    ):
        await registry.register(sample_entry)
        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event.type == EventType.CAPABILITY_REGISTERED
        assert event.payload["capability"] == "ai_coder"
        assert event.payload["kind"] == "ai_provider"
        assert event.payload["healthy"] is True

    async def test_register_overwrites_existing(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)

        updated = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=False,
            roles=[Role.CODER],
            provider_name="openrouter-v2",
        )
        await registry.register(updated)

        entry = registry.get(Capability.AI_CODER)
        assert entry.provider_name == "openrouter-v2"
        assert entry.healthy is False


# --- Test deregister() ---


class TestDeregister:
    async def test_deregister_removes_capability(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        await registry.deregister(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)

    async def test_deregister_emits_event(
        self,
        registry: CapabilityRegistry,
        sample_entry: CapabilityEntry,
        emitted_events: list[Event],
    ):
        await registry.register(sample_entry)
        emitted_events.clear()

        await registry.deregister(Capability.AI_CODER)
        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event.type == EventType.CAPABILITY_DEREGISTERED
        assert event.payload["capability"] == "ai_coder"

    async def test_deregister_raises_if_not_registered(
        self, registry: CapabilityRegistry
    ):
        with pytest.raises(CapabilityUnavailableError) as exc_info:
            await registry.deregister(Capability.AI_CODER)
        assert exc_info.value.identifier == "ai_coder"


# --- Test get() ---


class TestGet:
    async def test_get_returns_entry(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        entry = registry.get(Capability.AI_CODER)
        assert entry == sample_entry

    def test_get_raises_if_not_registered(self, registry: CapabilityRegistry):
        with pytest.raises(CapabilityUnavailableError) as exc_info:
            registry.get(Capability.AI_CODER)
        assert exc_info.value.identifier == "ai_coder"
        assert exc_info.value.kind == "capability"


# --- Test has() ---


class TestHas:
    async def test_has_true_when_registered(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        assert registry.has(Capability.AI_CODER) is True

    def test_has_false_when_not_registered(self, registry: CapabilityRegistry):
        assert registry.has(Capability.AI_CODER) is False


# --- Test has_kind() ---


class TestHasKind:
    async def test_has_kind_true_when_present(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        assert registry.has_kind(CapabilityKind.AI_PROVIDER) is True

    def test_has_kind_false_when_absent(self, registry: CapabilityRegistry):
        assert registry.has_kind(CapabilityKind.AI_PROVIDER) is False

    async def test_has_kind_after_deregister(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        await registry.deregister(Capability.AI_CODER)
        assert registry.has_kind(CapabilityKind.AI_PROVIDER) is False


# --- Test any_for_role() ---


class TestAnyForRole:
    async def test_any_for_role_true(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        assert registry.any_for_role(Role.CODER) is True

    def test_any_for_role_false(self, registry: CapabilityRegistry):
        assert registry.any_for_role(Role.CODER) is False

    async def test_any_for_role_ignores_other_roles(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        assert registry.any_for_role(Role.REVIEWER) is False


# --- Test healthy_for_role() ---


class TestHealthyForRole:
    async def test_returns_healthy_entries(
        self, registry: CapabilityRegistry, sample_entry: CapabilityEntry
    ):
        await registry.register(sample_entry)
        results = registry.healthy_for_role(Role.CODER)
        assert len(results) == 1
        assert results[0] == sample_entry

    async def test_excludes_unhealthy(self, registry: CapabilityRegistry):
        unhealthy = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=False,
            roles=[Role.CODER],
            provider_name="openrouter",
        )
        await registry.register(unhealthy)
        results = registry.healthy_for_role(Role.CODER)
        assert len(results) == 0

    async def test_returns_empty_for_no_match(self, registry: CapabilityRegistry):
        results = registry.healthy_for_role(Role.CODER)
        assert results == []

    async def test_multiple_healthy_for_role(self, registry: CapabilityRegistry):
        entry1 = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
            provider_name="provider-a",
        )
        entry2 = CapabilityEntry(
            name=Capability.AI_ARCHITECT,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER, Role.ARCHITECT],
            provider_name="provider-b",
        )
        await registry.register(entry1)
        await registry.register(entry2)
        results = registry.healthy_for_role(Role.CODER)
        assert len(results) == 2


# --- Test summary() ---


class TestSummary:
    def test_empty_registry_summary(self, registry: CapabilityRegistry):
        summary = registry.summary()
        assert summary.can_operate is False
        assert len(summary.missing_required) > 0
        assert len(summary.available) == 0

    async def test_operational_summary(
        self,
        registry: CapabilityRegistry,
        sample_entry: CapabilityEntry,
        vcs_entry: CapabilityEntry,
        tool_entry: CapabilityEntry,
    ):
        await registry.register(sample_entry)
        await registry.register(vcs_entry)
        await registry.register(tool_entry)

        summary = registry.summary()
        assert summary.can_operate is True
        assert len(summary.missing_required) == 0
        assert Capability.AI_CODER in summary.available
        assert Capability.VCS_GITHUB in summary.available
        assert Capability.TOOL_AIDER in summary.available

    async def test_degraded_summary(
        self,
        registry: CapabilityRegistry,
        vcs_entry: CapabilityEntry,
        tool_entry: CapabilityEntry,
    ):
        # Register unhealthy coder
        unhealthy_coder = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=False,
            roles=[Role.CODER],
            provider_name="openrouter",
        )
        await registry.register(unhealthy_coder)
        await registry.register(vcs_entry)
        await registry.register(tool_entry)

        summary = registry.summary()
        # Can't operate because healthy_for_role(CODER) is empty,
        # but any_for_role(CODER) is True since the entry exists
        # Note: can_operate checks any_for_role, not healthy_for_role
        assert summary.can_operate is True
        assert Capability.AI_CODER in summary.degraded


# --- Test concurrency (asyncio.Lock) ---


class TestConcurrency:
    async def test_concurrent_registers_are_safe(self, registry: CapabilityRegistry):
        """Concurrent register calls should not corrupt state."""
        entries = [
            CapabilityEntry(
                name=Capability.AI_CODER,
                kind=CapabilityKind.AI_PROVIDER,
                healthy=True,
                roles=[Role.CODER],
                provider_name=f"provider-{i}",
            )
            for i in range(10)
        ]

        await asyncio.gather(*(registry.register(e) for e in entries))

        # The last write wins — the entry should be present
        assert registry.has(Capability.AI_CODER)

    async def test_concurrent_register_deregister(self, registry: CapabilityRegistry):
        """Mixed concurrent register/deregister should not raise unhandled errors."""
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
            provider_name="test",
        )
        await registry.register(entry)

        async def register_loop():
            for _ in range(5):
                await registry.register(entry)

        async def deregister_loop():
            for _ in range(5):
                try:
                    await registry.deregister(Capability.AI_CODER)
                except CapabilityUnavailableError:
                    pass  # Expected when already deregistered

        await asyncio.gather(register_loop(), deregister_loop())
        # After concurrent mutations, state should be consistent
        # (either present or not — no corruption)


# --- Test without event emitter ---


class TestWithoutEventEmitter:
    async def test_register_without_emitter(self):
        """Registry works even without an event emitter configured."""
        registry = CapabilityRegistry(event_emitter=None)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
            provider_name="test",
        )
        await registry.register(entry)
        assert registry.has(Capability.AI_CODER)

    async def test_deregister_without_emitter(self):
        """Deregister works without an event emitter."""
        registry = CapabilityRegistry(event_emitter=None)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
            provider_name="test",
        )
        await registry.register(entry)
        await registry.deregister(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)


# --- Test CapabilityUnavailableError ---


class TestCapabilityUnavailableError:
    def test_error_message(self):
        err = CapabilityUnavailableError("ai_coder")
        assert "ai_coder" in str(err)
        assert err.identifier == "ai_coder"
        assert err.kind == "capability"

    def test_error_with_role_kind(self):
        err = CapabilityUnavailableError("coder", kind="role")
        assert "Role unavailable: coder" in str(err)
        assert err.kind == "role"


# --- Test resolve-by-role scenarios (Req 15.2) ---


class TestResolveByRole:
    async def test_healthy_for_role_returns_only_healthy_entries_for_role(
        self, registry: CapabilityRegistry
    ):
        """Resolve by role returns only healthy entries matching the role."""
        healthy_entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER, Role.PLANNER],
            provider_name="provider-a",
        )
        unhealthy_entry = CapabilityEntry(
            name=Capability.AI_ARCHITECT,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=False,
            roles=[Role.CODER],
            provider_name="provider-b",
        )
        await registry.register(healthy_entry)
        await registry.register(unhealthy_entry)

        results = registry.healthy_for_role(Role.CODER)
        assert len(results) == 1
        assert results[0].provider_name == "provider-a"

    async def test_all_providers_for_role_unhealthy_returns_empty(
        self, registry: CapabilityRegistry
    ):
        """When all providers for a role are unhealthy, resolution returns empty list."""
        unhealthy1 = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=False,
            roles=[Role.CODER],
            provider_name="provider-a",
        )
        unhealthy2 = CapabilityEntry(
            name=Capability.AI_ARCHITECT,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=False,
            roles=[Role.CODER],
            provider_name="provider-b",
        )
        await registry.register(unhealthy1)
        await registry.register(unhealthy2)

        results = registry.healthy_for_role(Role.CODER)
        assert results == []
        # But the role still exists in the registry (any_for_role is True)
        assert registry.any_for_role(Role.CODER) is True
