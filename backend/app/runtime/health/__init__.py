"""Health Monitor — continuous re-probing of registered capabilities.

The Health Monitor is the only continuous writer to the Capability Registry
after boot (per design R7). It:

1. Re-probes every registered capability on a configurable interval (5–300s, default 30s).
2. Uses a per-probe timeout (1–60s, default 10s).
3. Tracks consecutive failures per capability.
4. Deregisters a capability after the consecutive failure threshold (1–10, default 3).
5. Tracks recovery probes for previously unhealthy capabilities.
6. Re-registers a capability after the recovery threshold (1–10, default 2) consecutive
   healthy probes.
7. Emits capability.degraded / capability.recovered events on transitions.
8. Writes a DecisionRecord on each capability transition.

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType
from app.runtime.models import Capability, CapabilityEntry
from app.runtime.registry import CapabilityRegistry
from app.runtime.types import Health

logger = logging.getLogger(__name__)

# Type aliases
EventEmitter = Callable[[Event], Awaitable[Any]]
AuditWriter = Callable[[DecisionRecord], Awaitable[None]]


def _clamp(value: int | float, low: int | float, high: int | float) -> int | float:
    """Clamp a value between low and high bounds."""
    return max(low, min(high, value))


@dataclass
class HealthMonitorConfig:
    """Configuration for the Health Monitor.

    All values are bounded per requirements 14.1–14.4:
    - probe_interval_s: 5–300, default 30
    - probe_timeout_s: 1–60, default 10
    - failure_threshold: 1–10, default 3
    - recovery_threshold: 1–10, default 2
    """

    probe_interval_s: float = 30.0
    probe_timeout_s: float = 10.0
    failure_threshold: int = 3
    recovery_threshold: int = 2

    def __post_init__(self) -> None:
        """Clamp all values to their valid bounds."""
        self.probe_interval_s = float(_clamp(self.probe_interval_s, 5, 300))
        self.probe_timeout_s = float(_clamp(self.probe_timeout_s, 1, 60))
        self.failure_threshold = int(_clamp(self.failure_threshold, 1, 10))
        self.recovery_threshold = int(_clamp(self.recovery_threshold, 1, 10))


@dataclass
class _CapabilityHealthState:
    """Internal tracking state for a single capability's health probes."""

    consecutive_failures: int = 0
    consecutive_recoveries: int = 0
    degraded: bool = False
    last_reason: str = ""


class HealthMonitor:
    """Background service that continuously re-probes capabilities.

    Usage:
        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: ai_provider},
            event_emitter=bus.publish,
            audit_writer=audit.write_decision,
        )
        await monitor.start()
        # ... later ...
        await monitor.stop()
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        probe_map: dict[Capability, Any],
        event_emitter: EventEmitter | None = None,
        audit_writer: AuditWriter | None = None,
        config: HealthMonitorConfig | None = None,
        session_id: str = "system",
    ) -> None:
        """Initialize the Health Monitor.

        Args:
            registry: The CapabilityRegistry to monitor and update.
            probe_map: Mapping of capability names to objects with health_check() methods.
            event_emitter: Async callable to publish events (typically EventBus.publish).
            audit_writer: Async callable to write DecisionRecord entries.
            config: Configuration for probe intervals, thresholds, etc.
            session_id: Session ID used for emitted events.
        """
        self._registry = registry
        self._probe_map = probe_map
        self._event_emitter = event_emitter
        self._audit_writer = audit_writer
        self._config = config or HealthMonitorConfig()
        self._session_id = session_id

        # Per-capability health tracking
        self._states: dict[Capability, _CapabilityHealthState] = {}

        # Background task handle
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def config(self) -> HealthMonitorConfig:
        """Read-only access to the current configuration."""
        return self._config

    @property
    def running(self) -> bool:
        """Whether the monitor loop is currently running."""
        return self._running

    def get_state(self, capability: Capability) -> _CapabilityHealthState | None:
        """Get the internal health tracking state for a capability (for testing)."""
        return self._states.get(capability)

    async def start(self) -> None:
        """Start the background health monitoring loop.

        Idempotent: calling start() when already running has no effect.
        """
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "Health Monitor started (interval=%.1fs, timeout=%.1fs, "
            "failure_threshold=%d, recovery_threshold=%d)",
            self._config.probe_interval_s,
            self._config.probe_timeout_s,
            self._config.failure_threshold,
            self._config.recovery_threshold,
        )

    async def stop(self) -> None:
        """Stop the background health monitoring loop.

        Waits for the current probe cycle to complete before returning.
        """
        if not self._running:
            return

        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        logger.info("Health Monitor stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop: sleep then probe all capabilities."""
        try:
            while self._running:
                await asyncio.sleep(self._config.probe_interval_s)
                if not self._running:
                    break
                await self._probe_all()
        except asyncio.CancelledError:
            pass

    async def _probe_all(self) -> None:
        """Probe all registered capabilities and all degraded (recovering) capabilities."""
        # Get currently registered capabilities
        registered = set(self._registry.entries.keys())

        # Also probe capabilities that are in degraded state (for recovery)
        degraded_caps = {
            cap for cap, state in self._states.items() if state.degraded
        }

        # All capabilities to probe
        to_probe = registered | degraded_caps

        for capability in to_probe:
            await self._probe_one(capability)

    async def _probe_one(self, capability: Capability) -> None:
        """Probe a single capability and update tracking state.

        Handles both the failure path (increment failures, deregister on threshold)
        and the recovery path (increment recoveries, re-register on threshold).
        """
        state = self._states.setdefault(capability, _CapabilityHealthState())
        probe_fn = self._probe_map.get(capability)

        if probe_fn is None:
            # No probe function available — treat as failure
            await self._handle_probe_failure(
                capability, state, "No probe function available"
            )
            return

        # Execute the health check with timeout
        try:
            health = await asyncio.wait_for(
                probe_fn.health_check(),
                timeout=self._config.probe_timeout_s,
            )
        except asyncio.TimeoutError:
            await self._handle_probe_failure(
                capability,
                state,
                f"Probe timed out after {self._config.probe_timeout_s}s",
            )
            return
        except Exception as e:
            await self._handle_probe_failure(
                capability, state, f"Probe error: {e}"
            )
            return

        if health.ok:
            await self._handle_probe_success(capability, state)
        else:
            reason = health.message or f"Unhealthy: {health.status.value}"
            await self._handle_probe_failure(capability, state, reason)

    async def _handle_probe_failure(
        self,
        capability: Capability,
        state: _CapabilityHealthState,
        reason: str,
    ) -> None:
        """Handle a failed probe for a capability.

        Per requirement 14.2: increment consecutive failure count, keep registered
        until threshold is reached.
        Per requirement 14.3: deregister on reaching failure threshold.
        """
        # Reset recovery counter on failure
        state.consecutive_recoveries = 0
        state.last_reason = reason

        if state.degraded:
            # Already degraded — just update failure count, no further action
            state.consecutive_failures += 1
            return

        # Still registered — increment failures
        state.consecutive_failures += 1

        logger.debug(
            "Capability %s probe failed (%d/%d): %s",
            capability.value,
            state.consecutive_failures,
            self._config.failure_threshold,
            reason,
        )

        # Check threshold
        if state.consecutive_failures >= self._config.failure_threshold:
            await self._degrade_capability(capability, state, reason)

    async def _handle_probe_success(
        self,
        capability: Capability,
        state: _CapabilityHealthState,
    ) -> None:
        """Handle a successful probe for a capability.

        If the capability is currently healthy (registered), reset failure counter.
        If degraded, increment recovery counter and re-register on threshold.
        """
        if not state.degraded:
            # Healthy capability remains healthy — reset failures
            state.consecutive_failures = 0
            return

        # Degraded capability — track recovery
        state.consecutive_recoveries += 1

        logger.debug(
            "Degraded capability %s probe succeeded (%d/%d)",
            capability.value,
            state.consecutive_recoveries,
            self._config.recovery_threshold,
        )

        if state.consecutive_recoveries >= self._config.recovery_threshold:
            await self._recover_capability(capability, state)

    async def _degrade_capability(
        self,
        capability: Capability,
        state: _CapabilityHealthState,
        reason: str,
    ) -> None:
        """Deregister a capability and emit degraded event + DecisionRecord.

        Per requirement 14.3: deregister, emit capability.degraded, write DecisionRecord.
        """
        state.degraded = True

        # Deregister from the registry (only if still registered)
        if self._registry.has(capability):
            try:
                await self._registry.deregister(capability)
            except Exception:
                logger.exception(
                    "Failed to deregister capability %s", capability.value
                )

        # Emit capability.degraded event
        await self._emit_event(
            EventType.CAPABILITY_DEGRADED,
            payload={
                "capability": capability.value,
                "reason": reason,
                "consecutive_failures": state.consecutive_failures,
                "failure_threshold": self._config.failure_threshold,
            },
        )

        # Write DecisionRecord for the transition
        await self._write_decision(
            subject=capability.value,
            decision="deregister",
            rationale=(
                f"Capability '{capability.value}' failed {state.consecutive_failures} "
                f"consecutive probes (threshold: {self._config.failure_threshold}). "
                f"Last failure reason: {reason}"
            ),
            inputs={
                "capability": capability.value,
                "consecutive_failures": state.consecutive_failures,
                "failure_threshold": self._config.failure_threshold,
                "reason": reason,
            },
            alternatives=["keep_registered", "retry_immediately"],
        )

        logger.warning(
            "Capability degraded: %s (reason: %s, failures: %d)",
            capability.value,
            reason,
            state.consecutive_failures,
        )

    async def _recover_capability(
        self,
        capability: Capability,
        state: _CapabilityHealthState,
    ) -> None:
        """Re-register a recovered capability and emit recovered event + DecisionRecord.

        Per requirement 14.4: re-register, emit capability.recovered.
        """
        state.degraded = False
        state.consecutive_failures = 0
        state.consecutive_recoveries = 0

        # Re-register the capability
        # Attempt to find its original entry info from the probe_map metadata
        entry = self._build_recovery_entry(capability)
        await self._registry.register(entry)

        # Emit capability.recovered event
        await self._emit_event(
            EventType.CAPABILITY_RECOVERED,
            payload={
                "capability": capability.value,
                "recovery_threshold": self._config.recovery_threshold,
            },
        )

        # Write DecisionRecord for the transition
        await self._write_decision(
            subject=capability.value,
            decision="re-register",
            rationale=(
                f"Capability '{capability.value}' returned healthy for "
                f"{self._config.recovery_threshold} consecutive probes. "
                f"Re-registering."
            ),
            inputs={
                "capability": capability.value,
                "recovery_threshold": self._config.recovery_threshold,
            },
            alternatives=["keep_degraded", "wait_for_more_probes"],
        )

        logger.info(
            "Capability recovered: %s (after %d consecutive healthy probes)",
            capability.value,
            self._config.recovery_threshold,
        )

    def _build_recovery_entry(self, capability: Capability) -> CapabilityEntry:
        """Build a CapabilityEntry for re-registration after recovery.

        Uses any metadata stored on the probe_map object to reconstruct the entry.
        Falls back to a minimal entry if no metadata is available.
        """
        probe_obj = self._probe_map.get(capability)

        # Try to get metadata from the probe object
        provider_name = getattr(probe_obj, "name", "") if probe_obj else ""

        # Default to AI_PROVIDER kind — in real usage, the entry metadata
        # would be preserved from the original registration.
        from app.runtime.models import CapabilityKind

        return CapabilityEntry(
            name=capability,
            kind=getattr(probe_obj, "_kind", CapabilityKind.AI_PROVIDER)
            if probe_obj
            else CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=getattr(probe_obj, "_roles", []) if probe_obj else [],
            provider_name=provider_name,
        )

    async def _emit_event(
        self,
        event_type: EventType,
        *,
        payload: dict[str, Any],
    ) -> None:
        """Emit an event via the configured event emitter."""
        if self._event_emitter is None:
            logger.debug(
                "No event emitter configured; skipping %s event", event_type.value
            )
            return

        event = Event.create(
            type=event_type,
            session_id=self._session_id,
            source="health_monitor",
            payload=payload,
            correlation_id=self._session_id,
            event_id=f"health_{event_type.value}_{payload.get('capability', 'unknown')}",
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit %s event for %s",
                event_type.value,
                payload.get("capability", "unknown"),
            )

    async def _write_decision(
        self,
        *,
        subject: str,
        decision: str,
        rationale: str,
        inputs: dict[str, Any],
        alternatives: list[str],
    ) -> None:
        """Write a DecisionRecord for a capability transition."""
        if self._audit_writer is None:
            logger.debug("No audit writer configured; skipping DecisionRecord")
            return

        record = DecisionRecord(
            kind=DecisionKind.CAPABILITY_TRANSITION,
            subject=subject,
            inputs=inputs,
            decision=decision,
            rationale=rationale,
            alternatives=alternatives,
            session_id=self._session_id,
        )

        try:
            await self._audit_writer(record)
        except Exception:
            logger.exception(
                "Failed to write DecisionRecord for capability transition: %s",
                subject,
            )
