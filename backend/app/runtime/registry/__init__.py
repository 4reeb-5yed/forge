"""Capability Registry - discovery, registration, health monitoring.

The authoritative record of which capabilities are available and healthy at the
current instant. Resolves capabilities by name and by role.

Per design R7: only Discovery (at boot) and Health Monitor (continuously) write
to the registry. All other components read only.

Mutations are guarded by an asyncio.Lock and emit typed events to the EventBus.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from app.runtime.events.models import Event, EventType
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    CapabilitySummary,
    Role,
)

logger = logging.getLogger(__name__)


class CapabilityUnavailableError(Exception):
    """Raised when a requested capability is not present in the registry.

    Per requirement 15.2: if a requested Capability is not present at resolution
    time, raise this error identifying the requested capability name or Role.
    """

    def __init__(self, identifier: str, *, kind: str = "capability") -> None:
        self.identifier = identifier
        self.kind = kind
        super().__init__(
            f"{kind.capitalize()} unavailable: {identifier}"
        )


# Type alias for the event emission callback
EventEmitter = Callable[[Event], Awaitable[Any]]


class CapabilityRegistry:
    """Authoritative source of truth for what capabilities are available now.

    Thread-safe via asyncio.Lock. Emits capability.registered and
    capability.deregistered events on mutations via the provided event emitter.

    Usage:
        bus = EventBus()
        registry = CapabilityRegistry(event_emitter=bus.publish, session_id="boot")
    """

    def __init__(
        self,
        *,
        event_emitter: EventEmitter | None = None,
        session_id: str = "system",
    ) -> None:
        """Initialize the registry.

        Args:
            event_emitter: Async callable that publishes Event objects (typically EventBus.publish).
            session_id: The session ID to use for emitted events (typically "system" for boot).
        """
        self._entries: dict[Capability, CapabilityEntry] = {}
        self._lock = asyncio.Lock()
        self._event_emitter = event_emitter
        self._session_id = session_id

    async def register(self, entry: CapabilityEntry) -> None:
        """Register a capability in the registry.

        Acquires the mutation lock, adds the entry, and emits a
        capability.registered event.

        Args:
            entry: The CapabilityEntry to register.
        """
        async with self._lock:
            self._entries[entry.name] = entry

        await self._emit_event(
            EventType.CAPABILITY_REGISTERED,
            payload={
                "capability": entry.name.value,
                "kind": entry.kind.value,
                "healthy": entry.healthy,
                "roles": [r.value for r in entry.roles],
                "provider_name": entry.provider_name,
            },
        )

        logger.info(
            "Capability registered: %s (kind=%s, healthy=%s)",
            entry.name.value,
            entry.kind.value,
            entry.healthy,
        )

    async def deregister(self, capability: Capability) -> None:
        """Remove a capability from the registry.

        Acquires the mutation lock, removes the entry, and emits a
        capability.deregistered event.

        Args:
            capability: The capability name to deregister.

        Raises:
            CapabilityUnavailableError: If the capability is not registered.
        """
        async with self._lock:
            if capability not in self._entries:
                raise CapabilityUnavailableError(capability.value)
            entry = self._entries.pop(capability)

        await self._emit_event(
            EventType.CAPABILITY_DEREGISTERED,
            payload={
                "capability": capability.value,
                "kind": entry.kind.value,
                "reason": "deregistered",
            },
        )

        logger.info("Capability deregistered: %s", capability.value)

    def get(self, capability: Capability) -> CapabilityEntry:
        """Resolve a capability by name.

        Args:
            capability: The capability to look up.

        Returns:
            The registered CapabilityEntry.

        Raises:
            CapabilityUnavailableError: If the capability is not registered.
        """
        if capability not in self._entries:
            raise CapabilityUnavailableError(capability.value)
        return self._entries[capability]

    def has(self, capability: Capability) -> bool:
        """Check whether a capability is currently registered.

        Args:
            capability: The capability to check.

        Returns:
            True if registered, False otherwise.
        """
        return capability in self._entries

    def has_kind(self, kind: CapabilityKind) -> bool:
        """Check whether any capability of the given kind is registered.

        Args:
            kind: The CapabilityKind to check for.

        Returns:
            True if at least one capability of this kind is registered.
        """
        return any(entry.kind == kind for entry in self._entries.values())

    def any_for_role(self, role: Role) -> bool:
        """Check whether any registered capability serves the given role.

        Args:
            role: The Role to check for.

        Returns:
            True if at least one capability lists this role.
        """
        return any(
            role in entry.roles for entry in self._entries.values()
        )

    def healthy_for_role(self, role: Role) -> list[CapabilityEntry]:
        """Return all healthy capabilities that serve the given role.

        Args:
            role: The Role to find healthy providers for.

        Returns:
            List of healthy CapabilityEntry instances serving the role,
            in insertion order.
        """
        return [
            entry
            for entry in self._entries.values()
            if role in entry.roles and entry.healthy
        ]

    def summary(self) -> CapabilitySummary:
        """Produce a CapabilitySummary snapshot of the current registry state.

        Evaluates available, degraded, and missing-required capabilities.
        A capability is degraded if registered but unhealthy.
        Missing-required capabilities are those needed for OPERATIONAL mode
        (Coder role, VCS connector, coding tool) that are not available.

        Returns:
            A CapabilitySummary reflecting the current state.
        """
        available: dict[Capability, str] = {}
        degraded: list[Capability] = []

        for cap, entry in self._entries.items():
            if entry.healthy:
                available[cap] = "healthy"
            else:
                available[cap] = "degraded"
                degraded.append(cap)

        # Determine missing required capabilities for operational mode
        missing_required: list[Capability] = []
        missing_reasons: dict[str, str] = {}

        # Requirement 16.1: Coder role + VCS connector + coding tool required
        has_coder_role = self.any_for_role(Role.CODER)
        has_vcs = self.has_kind(CapabilityKind.VCS_CONNECTOR)
        has_coding_tool = self.has_kind(CapabilityKind.CODING_TOOL)

        if not has_coder_role:
            missing_required.append(Capability.AI_CODER)
            missing_reasons["coder_role"] = "No provider can serve the Coder role"
        if not has_vcs:
            missing_required.append(Capability.VCS_GITHUB)
            missing_reasons["vcs_connector"] = "No VCS connector is available"
        if not has_coding_tool:
            missing_required.append(Capability.TOOL_AIDER)
            missing_reasons["coding_tool"] = "No coding tool is available"

        can_operate = has_coder_role and has_vcs and has_coding_tool
        mode = "operational" if can_operate else "degraded"

        return CapabilitySummary(
            available=available,
            degraded=degraded,
            missing_required=missing_required,
            missing_reasons=missing_reasons,
            mode=mode,
            can_operate=can_operate,
        )

    @property
    def entries(self) -> dict[Capability, CapabilityEntry]:
        """Read-only access to the current entries (for inspection/testing)."""
        return dict(self._entries)

    async def _emit_event(
        self,
        event_type: EventType,
        *,
        payload: dict[str, Any],
    ) -> None:
        """Emit an event via the configured event emitter.

        If no emitter is configured, logs a debug message and returns.
        """
        if self._event_emitter is None:
            logger.debug(
                "No event emitter configured; skipping %s event", event_type.value
            )
            return

        event = Event.create(
            type=event_type,
            session_id=self._session_id,
            source="capability_registry",
            payload=payload,
            correlation_id=self._session_id,
            event_id=f"{event_type.value}_{payload.get('capability', 'unknown')}",
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit %s event for %s",
                event_type.value,
                payload.get("capability", "unknown"),
            )
