"""Audit Trail — persisted event-stream projection and DecisionRecord store.

The AuditTrail is an EventBus subscriber (pattern "*") that persists every
event to an audit_log store with a unique key on (session_id, seq). It also
stores DecisionRecord entries for explainability.

Requirements: 19.1, 19.2, 19.3, 19.5, 19.6
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.runtime.events.models import DecisionKind, DecisionRecord, Event

logger = logging.getLogger(__name__)


class AuditWriteError(Exception):
    """Raised when an audit trail write fails."""

    def __init__(self, message: str, event: Event | None = None) -> None:
        super().__init__(message)
        self.event = event


@dataclass
class AuditEntry:
    """A single persisted audit log entry."""

    session_id: str
    seq: int
    event: Event


class AuditTrail:
    """Persisted projection of the event stream plus DecisionRecord store.

    Subscribes to all events on the EventBus (pattern "*") and persists each
    to an in-memory audit_log table with unique key (session_id, seq).

    Provides query methods for events and decisions by session.

    Requirements:
        19.1 — Write DecisionRecord with kind, subject, inputs, decision,
                rationale, alternatives, caused_by_event_id
        19.2 — Deterministic rationale from inputs only (no AI prose)
        19.3 — Persist event stream with unique key (session_id, seq)
        19.5 — Retain event on write failure; surface error
        19.6 — Reject duplicate (session_id, seq) idempotently
    """

    def __init__(self) -> None:
        # Audit log: keyed by session_id -> list of AuditEntry (ordered by seq)
        self._audit_log: dict[str, list[AuditEntry]] = defaultdict(list)

        # Set of (session_id, seq) for deduplication
        self._persisted_keys: set[tuple[str, int]] = set()

        # Decision records: keyed by session_id -> list of DecisionRecord
        self._decisions: dict[str, list[DecisionRecord]] = defaultdict(list)

        # Retained events that failed to persist (for retry)
        self._retained_events: list[Event] = []

        # Errors surfaced during writes
        self._write_errors: list[AuditWriteError] = []

    async def handle_event(self, event: Event) -> None:
        """EventBus subscriber handler — persists each event to the audit log.

        This is the handler registered with the EventBus via subscribe("*", ...).

        On duplicate (session_id, seq): silently skips (idempotent, Req 19.6).
        On write failure: retains the event and surfaces the error (Req 19.5).

        Args:
            event: The event to persist.
        """
        session_id = event.correlation_id or event.session_id
        key = (session_id, event.seq)

        # Req 19.6: Reject duplicate (session_id, seq) idempotently
        if key in self._persisted_keys:
            return

        try:
            self._persist_event(session_id, event)
        except Exception as exc:
            # Req 19.5: Retain originating event on write failure; surface error
            error = AuditWriteError(
                f"Failed to persist audit entry for session={session_id}, "
                f"seq={event.seq}: {exc}",
                event=event,
            )
            self._retained_events.append(event)
            self._write_errors.append(error)
            logger.error(str(error))
            raise error from exc

    def _persist_event(self, session_id: str, event: Event) -> None:
        """Persist an event to the in-memory audit log.

        Args:
            session_id: The resolved session ID.
            event: The event to persist.
        """
        key = (session_id, event.seq)
        entry = AuditEntry(session_id=session_id, seq=event.seq, event=event)
        self._audit_log[session_id].append(entry)
        self._persisted_keys.add(key)

    def write_decision(
        self,
        *,
        kind: DecisionKind,
        subject: str,
        inputs: dict[str, Any],
        decision: str,
        alternatives: list[str] | None = None,
        caused_by_event_id: str | None = None,
        session_id: str = "",
    ) -> DecisionRecord:
        """Write a DecisionRecord to the audit trail.

        Constructs a deterministic rationale from the inputs only (Req 19.2).
        No AI-generated prose is used.

        Args:
            kind: The kind of decision (from DecisionKind enum).
            subject: What the decision is about.
            inputs: The inputs that informed the decision.
            decision: The decision made.
            alternatives: Alternative decisions that were considered.
            caused_by_event_id: The event_id of the event that caused this decision.
            session_id: The session this decision belongs to.

        Returns:
            The created DecisionRecord.

        Raises:
            AuditWriteError: If the decision cannot be persisted.
        """
        # Req 19.2: Deterministic rationale from inputs only
        rationale = self._build_rationale(kind, subject, inputs, decision)

        record = DecisionRecord(
            kind=kind,
            subject=subject,
            inputs=inputs,
            decision=decision,
            rationale=rationale,
            alternatives=alternatives or [],
            session_id=session_id,
        )

        try:
            self._decisions[session_id].append(record)
        except Exception as exc:
            error = AuditWriteError(
                f"Failed to write DecisionRecord for session={session_id}, "
                f"subject={subject}: {exc}",
                event=None,
            )
            self._write_errors.append(error)
            logger.error(str(error))
            raise error from exc

        return record

    @staticmethod
    def _build_rationale(
        kind: DecisionKind,
        subject: str,
        inputs: dict[str, Any],
        decision: str,
    ) -> str:
        """Build a deterministic rationale string from inputs only.

        Req 19.2: Identical inputs always produce an identical rationale.
        No AI-generated prose. Template-based construction.

        Args:
            kind: The decision kind.
            subject: The decision subject.
            inputs: The decision inputs.
            decision: The decision made.

        Returns:
            A deterministic rationale string.
        """
        # Build a sorted, deterministic representation of inputs
        input_parts: list[str] = []
        for key in sorted(inputs.keys()):
            input_parts.append(f"{key}={inputs[key]}")
        inputs_str = ", ".join(input_parts) if input_parts else "none"

        return (
            f"Decision '{decision}' for {kind.value} on '{subject}' "
            f"based on inputs: [{inputs_str}]"
        )

    # --- Query Methods ---

    def get_events(self, session_id: str) -> list[Event]:
        """Get all persisted events for a session, ordered by seq.

        Args:
            session_id: The session to query.

        Returns:
            List of events ordered by seq.
        """
        entries = self._audit_log.get(session_id, [])
        return [entry.event for entry in sorted(entries, key=lambda e: e.seq)]

    def get_decisions(self, session_id: str) -> list[DecisionRecord]:
        """Get all DecisionRecords for a session.

        Args:
            session_id: The session to query.

        Returns:
            List of DecisionRecords in insertion order.
        """
        return list(self._decisions.get(session_id, []))

    def get_decision_by_subject(
        self, session_id: str, subject: str
    ) -> DecisionRecord | None:
        """Get the most recent DecisionRecord for a given subject in a session.

        Args:
            session_id: The session to query.
            subject: The decision subject to look up.

        Returns:
            The most recent DecisionRecord matching the subject, or None.
        """
        decisions = self._decisions.get(session_id, [])
        # Return the most recent match
        for record in reversed(decisions):
            if record.subject == subject:
                return record
        return None

    def get_retained_events(self) -> list[Event]:
        """Get events that failed to persist and are retained for retry.

        Returns:
            List of retained events.
        """
        return list(self._retained_events)

    def get_write_errors(self) -> list[AuditWriteError]:
        """Get all write errors that have been surfaced.

        Returns:
            List of AuditWriteError instances.
        """
        return list(self._write_errors)

    def clear_session(self, session_id: str) -> None:
        """Clear all audit data for a session.

        Args:
            session_id: The session to clear.
        """
        self._audit_log.pop(session_id, None)
        self._decisions.pop(session_id, None)
        self._persisted_keys = {
            (sid, seq) for sid, seq in self._persisted_keys if sid != session_id
        }

    def subscribe_to_bus(self, bus: Any) -> None:
        """Register this AuditTrail as a subscriber on the given EventBus.

        Subscribes with pattern "*" to receive all events.

        Args:
            bus: An EventBus instance with a subscribe() method.
        """
        bus.subscribe(
            pattern="*",
            handler=self.handle_event,
            subscriber_id="audit_trail",
        )
