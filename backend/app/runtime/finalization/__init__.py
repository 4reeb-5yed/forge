"""Finalization Workflow — push to VCS and emit build summary.

Handles the final stage of the build workflow:
- Push canonical repo to VCS after all tasks have been processed
- Emit `build.done` event with summary, committed tasks, and skipped tasks
- Handle push failure: retain commits, emit error, do not emit `build.done`
- Handle approval timeout/rejection: do not perform action, emit event

Requirements: 9.3, 9.6, 9.7
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.runtime.events.models import DecisionKind, Event, EventType

logger = logging.getLogger(__name__)

# Default approval timeout in seconds
DEFAULT_APPROVAL_TIMEOUT = 300  # 5 minutes


class ApprovalStatus(str, Enum):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class PushFailureError(Exception):
    """Raised when pushing to VCS fails."""

    def __init__(self, reason: str, session_id: str) -> None:
        self.reason = reason
        self.session_id = session_id
        super().__init__(f"Push to VCS failed for session {session_id}: {reason}")


@dataclass
class BuildSummary:
    """Summary of a completed build for the build.done event.

    Attributes:
        session_id: The session that was built.
        total_tasks: Total number of tasks in the build.
        committed_tasks: Task IDs that were successfully committed.
        skipped_tasks: Task IDs that were skipped (dep failure, policy skip, etc.).
        failed_tasks: Task IDs that failed.
        push_sha: The SHA after push to VCS (if successful).
    """

    session_id: str = ""
    total_tasks: int = 0
    committed_tasks: list[str] = field(default_factory=list)
    skipped_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    push_sha: str = ""

    @property
    def summary_text(self) -> str:
        """Generate a human-readable summary of the build."""
        committed = len(self.committed_tasks)
        skipped = len(self.skipped_tasks)
        failed = len(self.failed_tasks)
        return (
            f"Build complete: {committed} committed, "
            f"{skipped} skipped, {failed} failed "
            f"out of {self.total_tasks} total tasks"
        )


class VCSPusher(Protocol):
    """Protocol for pushing to VCS.

    The FinalizationWorkflow uses this to push committed changes to the remote.
    """

    async def push(self, repo_path: str) -> None:
        """Push commits to the remote VCS.

        Args:
            repo_path: Path to the canonical repository.

        Raises:
            Exception: If push fails.
        """
        ...


class EventPublisher(Protocol):
    """Protocol for publishing events to the event bus."""

    async def publish(self, event: Event) -> Event:
        """Publish an event to the bus."""
        ...


class AuditWriter(Protocol):
    """Protocol for writing decision records to the audit trail."""

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
    ) -> Any:
        """Write a decision record."""
        ...


@dataclass
class ApprovalRequest:
    """Tracks a pending approval request."""

    request_id: str
    action: str
    session_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def approve(self) -> None:
        """Mark the request as approved and signal waiting coroutines."""
        self.status = ApprovalStatus.APPROVED
        self._event.set()

    def reject(self) -> None:
        """Mark the request as rejected and signal waiting coroutines."""
        self.status = ApprovalStatus.REJECTED
        self._event.set()

    async def wait(self, timeout: float) -> ApprovalStatus:
        """Wait for approval/rejection or timeout.

        Args:
            timeout: Maximum seconds to wait for a response.

        Returns:
            The final approval status.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.status = ApprovalStatus.TIMED_OUT
        return self.status


class FinalizationWorkflow:
    """Handles pushing committed changes to VCS and emitting build summary events.

    After all tasks have been processed (committed, skipped, or failed),
    the finalization workflow:
    1. Optionally requests approval (if push is approval-gated)
    2. Pushes the canonical repository to the VCS
    3. Emits a `build.done` event with the build summary

    On push failure:
    - Retains committed changes in the canonical repository
    - Emits an error event indicating the push failure
    - Does NOT emit a `build.done` event

    On approval timeout/rejection:
    - Does NOT perform the push
    - Emits an event indicating the action was not approved

    Requirements:
        9.3 — Push canonical repo and emit build.done with summary
        9.6 — On push failure: retain commits, emit error, no build.done
        9.7 — On approval rejection/timeout: don't perform action, emit event
    """

    def __init__(
        self,
        event_publisher: EventPublisher,
        vcs_pusher: VCSPusher,
        audit_writer: AuditWriter,
        session_id: str,
        canonical_path: str,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT,
        push_requires_approval: bool = False,
    ) -> None:
        """Initialize the FinalizationWorkflow.

        Args:
            event_publisher: Publisher for emitting events to the bus.
            vcs_pusher: VCS connector for pushing to remote.
            audit_writer: Audit trail for recording decisions.
            session_id: The session being finalized.
            canonical_path: Path to the canonical repository.
            approval_timeout: Seconds to wait for approval before timeout.
            push_requires_approval: Whether push is approval-gated.
        """
        self._event_publisher = event_publisher
        self._vcs_pusher = vcs_pusher
        self._audit_writer = audit_writer
        self._session_id = session_id
        self._canonical_path = canonical_path
        self._approval_timeout = approval_timeout
        self._push_requires_approval = push_requires_approval

        # Track pending approval requests
        self._pending_approvals: dict[str, ApprovalRequest] = {}

    @property
    def session_id(self) -> str:
        """The session being finalized."""
        return self._session_id

    @property
    def canonical_path(self) -> str:
        """Path to the canonical repository."""
        return self._canonical_path

    @property
    def pending_approvals(self) -> dict[str, ApprovalRequest]:
        """Currently pending approval requests."""
        return dict(self._pending_approvals)

    def respond_to_approval(self, request_id: str, approved: bool) -> bool:
        """Respond to a pending approval request.

        Args:
            request_id: The approval request ID to respond to.
            approved: True to approve, False to reject.

        Returns:
            True if the request was found and responded to, False otherwise.
        """
        request = self._pending_approvals.get(request_id)
        if request is None or request.status != ApprovalStatus.PENDING:
            return False

        if approved:
            request.approve()
        else:
            request.reject()
        return True

    async def finalize(
        self,
        committed_tasks: list[str],
        skipped_tasks: list[str],
        failed_tasks: list[str],
        total_tasks: int | None = None,
    ) -> BuildSummary | None:
        """Execute the finalization workflow.

        Pushes the canonical repository to VCS and emits a build.done event.
        If push is approval-gated, requests approval first.

        Args:
            committed_tasks: List of task IDs that were committed.
            skipped_tasks: List of task IDs that were skipped.
            failed_tasks: List of task IDs that failed.
            total_tasks: Total number of tasks (defaults to sum of all lists).

        Returns:
            BuildSummary if finalization succeeded, None if push failed or
            approval was not granted.
        """
        if total_tasks is None:
            total_tasks = len(committed_tasks) + len(skipped_tasks) + len(failed_tasks)

        # Step 1: Check if push requires approval (Req 9.7)
        if self._push_requires_approval:
            approved = await self._request_approval("push_to_vcs")
            if not approved:
                # Approval not granted — do not push, emit event
                return None

        # Step 2: Push canonical repo to VCS (Req 9.3)
        try:
            await self._vcs_pusher.push(self._canonical_path)
        except Exception as exc:
            # Req 9.6: Push failure — retain commits, emit error, no build.done
            await self._emit_push_failure(str(exc))

            # Record the push failure decision
            self._audit_writer.write_decision(
                kind=DecisionKind.TASK_OUTCOME,
                subject="push_to_vcs",
                inputs={
                    "session_id": self._session_id,
                    "canonical_path": self._canonical_path,
                    "error": str(exc),
                    "committed_tasks": committed_tasks,
                },
                decision="push_failed",
                alternatives=["retry_push"],
                session_id=self._session_id,
            )

            return None

        # Step 3: Emit build.done event (Req 9.3)
        summary = BuildSummary(
            session_id=self._session_id,
            total_tasks=total_tasks,
            committed_tasks=list(committed_tasks),
            skipped_tasks=list(skipped_tasks),
            failed_tasks=list(failed_tasks),
        )

        await self._emit_build_done(summary)

        return summary

    async def _request_approval(self, action: str) -> bool:
        """Request approval for a gated action.

        Pauses before performing the action, emits a pending event, and waits
        for user response or timeout.

        Args:
            action: The action requiring approval (e.g. "push_to_vcs").

        Returns:
            True if approved, False if rejected or timed out.
        """
        request_id = str(uuid.uuid4())
        request = ApprovalRequest(
            request_id=request_id,
            action=action,
            session_id=self._session_id,
        )
        self._pending_approvals[request_id] = request

        # Emit approval pending event (Req 9.4 from commit workflow, applied here)
        await self._emit_approval_pending(request_id, action)

        # Record the approval gate decision
        self._audit_writer.write_decision(
            kind=DecisionKind.APPROVAL_GATE,
            subject=action,
            inputs={
                "session_id": self._session_id,
                "action": action,
                "timeout_seconds": self._approval_timeout,
            },
            decision="approval_requested",
            alternatives=["skip_approval", "auto_approve"],
            session_id=self._session_id,
        )

        # Wait for approval or timeout (Req 9.7)
        status = await request.wait(self._approval_timeout)

        # Clean up
        self._pending_approvals.pop(request_id, None)

        if status == ApprovalStatus.APPROVED:
            return True

        # Req 9.7: Rejected or timed out — emit not-approved event
        await self._emit_approval_not_granted(request_id, action, status)

        # Record the rejection/timeout decision
        self._audit_writer.write_decision(
            kind=DecisionKind.APPROVAL_GATE,
            subject=action,
            inputs={
                "session_id": self._session_id,
                "action": action,
                "status": status.value,
            },
            decision=f"action_not_approved_{status.value}",
            alternatives=["approve", "retry_approval"],
            session_id=self._session_id,
        )

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Event emission helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _emit_build_done(self, summary: BuildSummary) -> None:
        """Emit a build.done event with the build summary.

        Req 9.3: Carries build summary, committed task IDs, skipped task IDs.
        """
        event = Event.create(
            type=EventType.BUILD_DONE,
            session_id=self._session_id,
            source="finalization_workflow",
            payload={
                "summary": summary.summary_text,
                "committed": summary.committed_tasks,
                "skipped": summary.skipped_tasks,
                "failed": summary.failed_tasks,
                "total_tasks": summary.total_tasks,
            },
            correlation_id=self._session_id,
            event_id=f"build-done-{self._session_id}-{uuid.uuid4().hex[:8]}",
        )
        await self._event_publisher.publish(event)

        logger.info(
            "Build finalized for session %s: %s",
            self._session_id,
            summary.summary_text,
        )

    async def _emit_push_failure(self, error_message: str) -> None:
        """Emit an error event indicating push failure.

        Req 9.6: Emit error event; do NOT emit build.done.
        """
        event = Event.create(
            type=EventType.ERROR,
            session_id=self._session_id,
            source="finalization_workflow",
            payload={
                "where": "finalization",
                "error_type": "push_failure",
                "message": f"Push to VCS failed: {error_message}",
                "recoverable": True,
            },
            correlation_id=self._session_id,
            event_id=f"push-fail-{self._session_id}-{uuid.uuid4().hex[:8]}",
        )
        await self._event_publisher.publish(event)

        logger.error(
            "Push to VCS failed for session %s: %s",
            self._session_id,
            error_message,
        )

    async def _emit_approval_pending(self, request_id: str, action: str) -> None:
        """Emit an event indicating approval is pending for an action.

        Req 9.4: Emit event indicating approval is pending.
        """
        event = Event.create(
            type=EventType.QUESTION,
            session_id=self._session_id,
            source="finalization_workflow",
            payload={
                "request_id": request_id,
                "action": action,
                "type": "approval_pending",
                "message": f"Approval required for action: {action}",
            },
            correlation_id=self._session_id,
            event_id=f"approval-pending-{request_id}",
        )
        await self._event_publisher.publish(event)

        logger.info(
            "Approval pending for action '%s' in session %s (request_id=%s)",
            action,
            self._session_id,
            request_id,
        )

    async def _emit_approval_not_granted(
        self, request_id: str, action: str, status: ApprovalStatus
    ) -> None:
        """Emit an event indicating the action was not approved.

        Req 9.7: Emit event indicating action was not approved.
        """
        event = Event.create(
            type=EventType.ERROR,
            session_id=self._session_id,
            source="finalization_workflow",
            payload={
                "where": "finalization",
                "error_type": "approval_not_granted",
                "request_id": request_id,
                "action": action,
                "status": status.value,
                "message": f"Action '{action}' was not approved: {status.value}",
                "recoverable": False,
            },
            correlation_id=self._session_id,
            event_id=f"approval-denied-{request_id}",
        )
        await self._event_publisher.publish(event)

        logger.warning(
            "Action '%s' not approved (status=%s) in session %s",
            action,
            status.value,
            self._session_id,
        )
