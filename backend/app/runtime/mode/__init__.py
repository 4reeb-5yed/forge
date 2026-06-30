"""Mode evaluation — minimum-capability validation and operational mode assessment.

Evaluates the current operational mode (OPERATIONAL or DEGRADED) from the
Capability Registry state.

Requirements covered:
- 16.1: Coder role + VCS connector + coding tool required for OPERATIONAL
- 16.2: DEGRADED mode rejects builds with precise missing-capability reasons
- 16.3: DEGRADED mode still serves chat/status/health/explain
- 16.4: Soft degradations recorded as DecisionRecord entries
- 16.5: Mode transitions emit capability transition event + DecisionRecord
- 16.6: Required capability loss triggers transition to DEGRADED
- 13.7: Emit forge.ready with operational mode, CapabilitySummary, startup report
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    CapabilitySummary,
    OperationalMode,
    Role,
)
from app.runtime.registry import CapabilityRegistry

logger = logging.getLogger(__name__)

# Type alias for the event emission callback
EventEmitter = Callable[[Event], Awaitable[Any]]


@dataclass
class ModeEvaluationResult:
    """Result of a mode evaluation containing the mode, summary, and any decisions."""

    mode: OperationalMode
    summary: CapabilitySummary
    decisions: list[DecisionRecord] = field(default_factory=list)
    startup_report: dict[str, Any] = field(default_factory=dict)


class ModeEvaluator:
    """Evaluates the operational mode from registry state.

    Determines whether the runtime can operate (OPERATIONAL) or must refuse
    builds (DEGRADED) based on the presence of minimum-required capabilities:
    - At least one healthy Coder role provider
    - At least one VCS connector
    - At least one coding tool

    Soft degradations (e.g., missing Reviewer role, missing vector store)
    are recorded as DecisionRecord entries but do not prevent operation.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        event_emitter: EventEmitter | None = None,
        session_id: str = "system",
    ) -> None:
        """Initialize the ModeEvaluator.

        Args:
            registry: The CapabilityRegistry to evaluate.
            event_emitter: Async callable for publishing events.
            session_id: Session ID for emitted events.
        """
        self._registry = registry
        self._event_emitter = event_emitter
        self._session_id = session_id
        self._current_mode: OperationalMode | None = None

    @property
    def current_mode(self) -> OperationalMode | None:
        """The last evaluated operational mode, or None if not yet evaluated."""
        return self._current_mode

    def evaluate(self) -> ModeEvaluationResult:
        """Evaluate the operational mode from current registry state.

        Checks minimum-required capabilities (Coder role, VCS connector, coding tool).
        Records soft degradations for optional capabilities.

        Returns:
            ModeEvaluationResult with mode, summary, and any decision records.
        """
        # Check required capabilities
        missing_reasons: dict[str, str] = {}

        has_coder_role = self._registry.any_for_role(Role.CODER)
        has_vcs = self._registry.has_kind(CapabilityKind.VCS_CONNECTOR)
        has_coding_tool = self._registry.has_kind(CapabilityKind.CODING_TOOL)

        if not has_coder_role:
            missing_reasons["coder_role"] = (
                "No provider can serve the Coder role"
            )
        if not has_vcs:
            missing_reasons["vcs_connector"] = (
                "No VCS connector is available"
            )
        if not has_coding_tool:
            missing_reasons["coding_tool"] = (
                "No coding tool is available"
            )

        can_operate = has_coder_role and has_vcs and has_coding_tool

        # Determine mode
        mode = OperationalMode.OPERATIONAL if can_operate else OperationalMode.DEGRADED

        # Check soft degradations (Reviewer role, vector store)
        decisions: list[DecisionRecord] = []
        soft_degradations: list[str] = []

        if can_operate:
            # Only record soft degradations when operational
            has_reviewer = self._registry.any_for_role(Role.REVIEWER)
            has_vector_store = self._registry.has_kind(CapabilityKind.VECTOR_STORE)

            if not has_reviewer:
                reason = "Reviewer role is unavailable; code review will be skipped"
                soft_degradations.append(reason)
                decisions.append(
                    DecisionRecord(
                        kind=DecisionKind.CAPABILITY_TRANSITION,
                        subject="reviewer_role",
                        inputs={
                            "role": Role.REVIEWER.value,
                            "available": False,
                        },
                        decision="soft_degradation",
                        rationale=reason,
                        alternatives=["wait for Reviewer provider to become healthy"],
                        session_id=self._session_id,
                    )
                )

            if not has_vector_store:
                reason = "Vector store is unavailable; context retrieval will use lexical fallback"
                soft_degradations.append(reason)
                decisions.append(
                    DecisionRecord(
                        kind=DecisionKind.CAPABILITY_TRANSITION,
                        subject="vector_store",
                        inputs={
                            "kind": CapabilityKind.VECTOR_STORE.value,
                            "available": False,
                        },
                        decision="soft_degradation",
                        rationale=reason,
                        alternatives=["wait for vector store to become healthy"],
                        session_id=self._session_id,
                    )
                )

        # Build summary from registry state
        entries = self._registry.entries
        available: dict[Capability, str] = {}
        degraded: list[Capability] = []
        missing_required: list[Capability] = []

        for cap, entry in entries.items():
            if entry.healthy:
                available[cap] = "healthy"
            else:
                available[cap] = "degraded"
                degraded.append(cap)

        # Add missing required capabilities
        if not has_coder_role:
            missing_required.append(Capability.AI_CODER)
        if not has_vcs:
            missing_required.append(Capability.VCS_GITHUB)
        if not has_coding_tool:
            missing_required.append(Capability.TOOL_AIDER)

        summary = CapabilitySummary(
            available=available,
            degraded=degraded,
            missing_required=missing_required,
            missing_reasons=missing_reasons,
            soft_degradations=soft_degradations,
            mode=mode.value,
            can_operate=can_operate,
        )

        # Build startup report
        startup_report: dict[str, Any] = {
            "mode": mode.value,
            "available_capabilities": len(available),
            "degraded_capabilities": len(degraded),
            "missing_required": [c.value for c in missing_required],
            "missing_reasons": missing_reasons,
            "soft_degradations": soft_degradations,
        }

        return ModeEvaluationResult(
            mode=mode,
            summary=summary,
            decisions=decisions,
            startup_report=startup_report,
        )

    async def evaluate_and_emit(self) -> ModeEvaluationResult:
        """Evaluate mode and emit transition events if mode changed.

        This is the primary entry point for mode evaluation. It:
        1. Evaluates the current mode from registry state
        2. Emits a capability transition event if the mode changed
        3. Records a DecisionRecord for mode transitions
        4. Emits forge.ready with the operational mode, summary, and startup report

        Returns:
            ModeEvaluationResult with mode, summary, decisions, and startup report.
        """
        result = self.evaluate()
        previous_mode = self._current_mode

        # Check if mode changed
        mode_changed = previous_mode is not None and previous_mode != result.mode

        if mode_changed:
            # Record mode transition as DecisionRecord
            transition_decision = DecisionRecord(
                kind=DecisionKind.CAPABILITY_TRANSITION,
                subject="operational_mode",
                inputs={
                    "previous_mode": previous_mode.value if previous_mode else None,
                    "new_mode": result.mode.value,
                    "missing_reasons": result.summary.missing_reasons,
                },
                decision=f"transition_to_{result.mode.value}",
                rationale=(
                    f"Operational mode changed from {previous_mode.value if previous_mode else 'none'} "
                    f"to {result.mode.value} due to capability state change"
                ),
                alternatives=[],
                session_id=self._session_id,
            )
            result.decisions.append(transition_decision)

            # Emit capability transition event
            await self._emit_event(
                EventType.CAPABILITY_DEGRADED
                if result.mode == OperationalMode.DEGRADED
                else EventType.CAPABILITY_RECOVERED,
                payload={
                    "previous_mode": previous_mode.value if previous_mode else None,
                    "new_mode": result.mode.value,
                    "missing_reasons": result.summary.missing_reasons,
                    "reason": f"Mode transition: {previous_mode.value if previous_mode else 'none'} -> {result.mode.value}",
                },
            )

        # Update current mode
        self._current_mode = result.mode

        # Emit forge.ready with operational mode, CapabilitySummary, and startup report
        await self._emit_forge_ready(result)

        return result

    async def _emit_forge_ready(self, result: ModeEvaluationResult) -> None:
        """Emit the forge.ready event with mode, summary, and startup report.

        Per requirement 13.7: After discovery_complete, emit forge.ready carrying
        the operational mode, Capability_Summary, and startup report.
        """
        payload: dict[str, Any] = {
            "mode": result.mode.value,
            "can_operate": result.summary.can_operate,
            "available_count": len(result.summary.available),
            "degraded_count": len(result.summary.degraded),
            "missing_required": [c.value for c in result.summary.missing_required],
            "missing_reasons": result.summary.missing_reasons,
            "soft_degradations": result.summary.soft_degradations,
            "startup_report": result.startup_report,
        }

        await self._emit_event(EventType.FORGE_READY, payload=payload)

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
            source="mode_evaluator",
            payload=payload,
            correlation_id=self._session_id,
            event_id=f"{event_type.value}_mode_evaluation",
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit %s event during mode evaluation",
                event_type.value,
            )
