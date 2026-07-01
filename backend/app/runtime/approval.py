"""Approval Gates - Human-in-the-loop approval for AI-generated changes.

This module provides the infrastructure for pausing builds before commits,
allowing humans to review diffs and approve/reject changes.

Requirements: Human approval gates for critical operations.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ApprovalStatus(Enum):
    """Status of an approval request."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalType(Enum):
    """Type of approval requested."""
    PRE_COMMIT = "pre_commit"  # Before committing changes
    PRE_PUSH = "pre_push"  # Before pushing to remote
    PRE_TASK = "pre_task"  # Before executing a task


@dataclass
class ApprovalRequest:
    """Represents a pending approval request."""
    id: str
    session_id: str
    task_id: str | None
    type: ApprovalType
    status: ApprovalStatus
    diff_summary: str
    diff_full: str | None
    changed_files: list[str]
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: datetime | None = None
    reviewer: str | None = None
    comment: str | None = None
    expires_at: datetime | None = None


@dataclass
class ApprovalResult:
    """Result of an approval decision."""
    request_id: str
    status: ApprovalStatus
    comment: str | None = None
    reviewed_at: datetime | None = None


# Global approval manager instance for easy access from API layer
_global_approval_manager: "ApprovalManager | None" = None


def get_approval_manager() -> "ApprovalManager":
    """Get the global approval manager instance."""
    global _global_approval_manager
    if _global_approval_manager is None:
        _global_approval_manager = ApprovalManager()
    return _global_approval_manager


def set_approval_manager(manager: "ApprovalManager") -> None:
    """Set the global approval manager instance."""
    global _global_approval_manager
    _global_approval_manager = manager


class ApprovalManager:
    """Manages approval requests and decisions for the build workflow.

    This enables human-in-the-loop pauses before commits, allowing users
    to review AI-generated changes before they are pushed to the repository.
    """

    def __init__(self, event_emitter: Any | None = None) -> None:
        """Initialize the ApprovalManager.

        Args:
            event_emitter: Optional event bus publisher for emitting approval events.
        """
        self._requests: dict[str, ApprovalRequest] = {}
        self._sessions: dict[str, list[str]] = {}  # session_id -> [request_ids]
        self._pending_events: dict[str, asyncio.Event] = {}  # request_id -> event
        self._results: dict[str, ApprovalResult] = {}
        self._event_emitter = event_emitter

    def update_event_emitter(self, event_emitter: Any) -> None:
        """Update the event emitter after initialization.

        Args:
            event_emitter: Event bus publisher function.
        """
        self._event_emitter = event_emitter
        logger.debug("ApprovalManager event emitter updated")

    async def create_request(
        self,
        session_id: str,
        task_id: str | None,
        approval_type: ApprovalType,
        diff_summary: str,
        diff_full: str | None = None,
        changed_files: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ApprovalRequest:
        """Create a new approval request.

        Args:
            session_id: The session this request belongs to.
            task_id: The task ID if this is task-related.
            approval_type: Type of approval being requested.
            diff_summary: Brief summary of changes.
            diff_full: Full diff content.
            changed_files: List of files that were changed.
            timeout_seconds: Optional timeout for auto-expiration.

        Returns:
            The created ApprovalRequest.
        """
        request_id = str(uuid.uuid4())

        expires_at = None
        if timeout_seconds:
            expires_at = datetime.now(timezone.utc).replace(
                microsecond=0
            )
            # Calculate expires_at manually (datetime doesn't support +timedelta easily)
            from datetime import timedelta
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)

        request = ApprovalRequest(
            id=request_id,
            session_id=session_id,
            task_id=task_id,
            type=approval_type,
            status=ApprovalStatus.PENDING,
            diff_summary=diff_summary,
            diff_full=diff_full,
            changed_files=changed_files or [],
            expires_at=expires_at,
        )

        self._requests[request_id] = request
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        self._sessions[session_id].append(request_id)
        self._pending_events[request_id] = asyncio.Event()

        # Emit approval request event
        if self._event_emitter:
            from app.runtime.events.models import Event, EventType
            event = Event.create(
                type=EventType.APPROVAL_REQUESTED,
                session_id=session_id,
                source="approval_manager",
                payload={
                    "request_id": request_id,
                    "task_id": task_id,
                    "approval_type": approval_type.value,
                    "diff_summary": diff_summary,
                    "changed_files": changed_files or [],
                },
                correlation_id=session_id,
                event_id=str(uuid.uuid4()),
            )
            await self._event_emitter(event)

        logger.info(
            "Approval request created: id=%s, session=%s, type=%s",
            request_id, session_id, approval_type.value
        )

        return request

    async def wait_for_decision(
        self,
        request_id: str,
        timeout_seconds: float = 300.0,
    ) -> ApprovalResult:
        """Wait for a decision on an approval request.

        Args:
            request_id: The approval request ID.
            timeout_seconds: Maximum time to wait.

        Returns:
            The approval result.

        Raises:
            TimeoutError: If timeout is reached.
        """
        if request_id not in self._pending_events:
            raise ValueError(f"Approval request not found: {request_id}")

        event = self._pending_events[request_id]

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            # Auto-expire the request
            await self._expire_request(request_id)
            raise TimeoutError(f"Approval request {request_id} timed out")

        if request_id not in self._results:
            raise ValueError(f"Approval result not found for {request_id}")

        return self._results[request_id]

    async def approve(
        self,
        request_id: str,
        reviewer: str = "human",
        comment: str | None = None,
    ) -> ApprovalResult:
        """Approve an approval request.

        Args:
            request_id: The approval request ID.
            reviewer: Who approved (default: "human").
            comment: Optional approval comment.

        Returns:
            The approval result.
        """
        return await self._make_decision(request_id, ApprovalStatus.APPROVED, reviewer, comment)

    async def reject(
        self,
        request_id: str,
        reviewer: str = "human",
        comment: str | None = None,
    ) -> ApprovalResult:
        """Reject an approval request.

        Args:
            request_id: The approval request ID.
            reviewer: Who rejected (default: "human").
            comment: Optional rejection reason.

        Returns:
            The approval result.
        """
        return await self._make_decision(request_id, ApprovalStatus.REJECTED, reviewer, comment)

    async def cancel(self, request_id: str) -> None:
        """Cancel an approval request.

        Args:
            request_id: The approval request ID.
        """
        if request_id not in self._requests:
            return

        request = self._requests[request_id]
        request.status = ApprovalStatus.CANCELLED

        if request_id in self._pending_events:
            self._pending_events[request_id].set()

        logger.info("Approval request cancelled: %s", request_id)

    async def _make_decision(
        self,
        request_id: str,
        status: ApprovalStatus,
        reviewer: str,
        comment: str | None,
    ) -> ApprovalResult:
        """Make a decision on an approval request.

        Args:
            request_id: The approval request ID.
            status: The decision status.
            reviewer: Who made the decision.
            comment: Optional comment.

        Returns:
            The approval result.
        """
        if request_id not in self._requests:
            raise ValueError(f"Approval request not found: {request_id}")

        request = self._requests[request_id]
        now = datetime.now(timezone.utc)

        request.status = status
        request.reviewed_at = now
        request.reviewer = reviewer
        request.comment = comment

        result = ApprovalResult(
            request_id=request_id,
            status=status,
            comment=comment,
            reviewed_at=now,
        )

        self._results[request_id] = result

        # Signal waiting coroutines
        if request_id in self._pending_events:
            self._pending_events[request_id].set()

        # Emit decision event
        if self._event_emitter:
            from app.runtime.events.models import Event, EventType

            event_type = EventType.APPROVAL_APPROVED if status == ApprovalStatus.APPROVED else EventType.APPROVAL_REJECTED

            event = Event.create(
                type=event_type,
                session_id=request.session_id,
                source="approval_manager",
                payload={
                    "request_id": request_id,
                    "status": status.value,
                    "reviewer": reviewer,
                    "comment": comment,
                    "task_id": request.task_id,
                },
                correlation_id=request.session_id,
                event_id=str(uuid.uuid4()),
            )
            await self._event_emitter(event)

        logger.info(
            "Approval decision made: id=%s, status=%s, reviewer=%s",
            request_id, status.value, reviewer
        )

        return result

    async def _expire_request(self, request_id: str) -> None:
        """Expire an approval request due to timeout.

        Args:
            request_id: The approval request ID.
        """
        if request_id not in self._requests:
            return

        request = self._requests[request_id]
        request.status = ApprovalStatus.EXPIRED

        if request_id in self._pending_events:
            self._pending_events[request_id].set()

        logger.warning("Approval request expired: %s", request_id)

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        """Get an approval request by ID.

        Args:
            request_id: The approval request ID.

        Returns:
            The request if found, None otherwise.
        """
        return self._requests.get(request_id)

    def get_pending_requests(self, session_id: str) -> list[ApprovalRequest]:
        """Get all pending approval requests for a session.

        Args:
            session_id: The session ID.

        Returns:
            List of pending approval requests.
        """
        if session_id not in self._sessions:
            return []

        return [
            self._requests[rid]
            for rid in self._sessions[session_id]
            if rid in self._requests and self._requests[rid].status == ApprovalStatus.PENDING
        ]

    def get_request_result(self, request_id: str) -> ApprovalResult | None:
        """Get the result of an approval request.

        Args:
            request_id: The approval request ID.

        Returns:
            The result if available, None otherwise.
        """
        return self._results.get(request_id)

    def get_all_requests(self, session_id: str) -> list[ApprovalRequest]:
        """Get all approval requests for a session.

        Args:
            session_id: The session ID.

        Returns:
            List of all approval requests.
        """
        if session_id not in self._sessions:
            return []

        return [
            self._requests[rid]
            for rid in self._sessions[session_id]
            if rid in self._requests
        ]
