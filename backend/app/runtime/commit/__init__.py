"""Commit Workflow — commits verified task changes with approval gates.

Orchestrates the commit of task changes from workspaces into the canonical
repository. Enforces that only verified tasks (those with `verify.passed`)
can be committed, supports approval-gated actions, and handles commit failures
gracefully.

Requirements: 9.1, 9.2, 9.4, 9.5
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType

logger = logging.getLogger(__name__)


class CommitStatus(str, Enum):
    """Status of a commit operation."""

    PENDING_VERIFICATION = "pending_verification"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    COMMITTED = "committed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass
class CommitRequest:
    """A request to commit a task's changes to the canonical repository.

    Attributes:
        task_id: The task whose changes to commit.
        session_id: The session this task belongs to.
        workspace_path: Path to the workspace containing the changes.
        changed_files: List of file paths that were modified.
        commit_message: The commit message to use.
        approval_required: Whether this commit requires explicit user approval.
    """

    task_id: str
    session_id: str
    workspace_path: str
    changed_files: list[str] = field(default_factory=list)
    commit_message: str = ""
    approval_required: bool = False


@dataclass
class CommitResult:
    """Result of a commit workflow execution.

    Attributes:
        task_id: The task that was committed.
        status: Final status of the commit.
        commit_sha: The SHA of the resulting commit (if successful).
        changed_files: List of committed file paths (if successful).
        error: Error message if commit failed or was rejected.
    """

    task_id: str
    status: CommitStatus
    commit_sha: str = ""
    changed_files: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def success(self) -> bool:
        """True if the commit was successful."""
        return self.status == CommitStatus.COMMITTED


class VCSCommitter(Protocol):
    """Protocol for the VCS commit operation.

    Abstracts the actual commit operation for testability.
    """

    async def commit(
        self, repo_path: str, message: str, files: list[str]
    ) -> str:
        """Commit specified files and return the commit SHA.

        Args:
            repo_path: Path to the repository.
            message: Commit message.
            files: List of file paths to commit.

        Returns:
            The commit SHA.

        Raises:
            Exception: If the commit fails.
        """
        ...


class ApprovalProvider(Protocol):
    """Protocol for requesting and waiting for user approval.

    Implementations may be sync (auto-approve for tests) or async
    (wait for user input in production).
    """

    async def request_approval(
        self, action: str, task_id: str, session_id: str, details: dict[str, Any]
    ) -> bool:
        """Request user approval for an action.

        Args:
            action: The action requiring approval (e.g., "commit").
            task_id: The task this approval is for.
            session_id: The session this belongs to.
            details: Additional details about what's being approved.

        Returns:
            True if approved, False if rejected or timed out.
        """
        ...


# Type alias for the event emission callback
EventEmitter = Callable[[Event], Awaitable[Any]]


class CommitNotVerifiedError(Exception):
    """Raised when attempting to commit a task that has not passed verification."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(
            f"Cannot commit task '{task_id}': verify.passed has not been emitted"
        )


class CommitFailedError(Exception):
    """Raised when a commit operation fails."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Commit failed for task '{task_id}': {reason}")


class CommitWorkflow:
    """Orchestrates committing verified task changes with approval gates.

    Enforces the invariant that only tasks with `verify.passed` can be
    committed. Supports approval-gated commits where user approval is
    required before the commit proceeds. Handles commit failures by
    leaving the canonical repository unchanged and emitting error events.

    Workflow:
    1. Verify that `verify.passed` has been emitted for the task.
    2. If approval-gated: pause, request approval, emit pending event.
    3. On approval (or if not gated): perform the commit.
    4. Emit `commit.done` with SHA and changed files on success.
    5. On failure: leave repo unchanged, emit error event.

    Requirements:
        9.1 — Commit on verify.passed; emit commit.done with SHA and files.
        9.2 — Never commit without verify.passed.
        9.4 — Approval-gated actions: pause, request, emit pending.
        9.5 — On commit failure: leave repo unchanged, emit error.
    """

    def __init__(
        self,
        *,
        vcs_committer: VCSCommitter,
        event_emitter: EventEmitter | None = None,
        approval_provider: ApprovalProvider | None = None,
        session_id: str = "",
        canonical_repo_path: str = "",
    ) -> None:
        """Initialize the CommitWorkflow.

        Args:
            vcs_committer: The VCS connector for performing commits.
            event_emitter: Async callable for publishing events.
            approval_provider: Provider for requesting user approval.
                If None, approval-gated commits will be auto-rejected.
            session_id: Session ID for emitted events.
            canonical_repo_path: Path to the canonical repository.
        """
        self._vcs_committer = vcs_committer
        self._event_emitter = event_emitter
        self._approval_provider = approval_provider
        self._session_id = session_id
        self._canonical_repo_path = canonical_repo_path

        # Track which tasks have passed verification
        self._verified_tasks: set[str] = set()

        # Track commit results for each task
        self._commit_results: dict[str, CommitResult] = {}

        # Decision records for audit trail
        self._decisions: list[DecisionRecord] = []

    @property
    def verified_tasks(self) -> set[str]:
        """Set of task IDs that have passed verification."""
        return set(self._verified_tasks)

    @property
    def commit_results(self) -> dict[str, CommitResult]:
        """All commit results keyed by task ID."""
        return dict(self._commit_results)

    @property
    def decisions(self) -> list[DecisionRecord]:
        """Decision records accumulated during workflow execution."""
        return list(self._decisions)

    def mark_verified(self, task_id: str) -> None:
        """Mark a task as having passed verification.

        Called when a `verify.passed` event is received for a task.
        This is a prerequisite for committing the task's changes.

        Args:
            task_id: The task that passed verification.
        """
        self._verified_tasks.add(task_id)

    def is_verified(self, task_id: str) -> bool:
        """Check if a task has passed verification.

        Args:
            task_id: The task to check.

        Returns:
            True if verify.passed has been received for this task.
        """
        return task_id in self._verified_tasks

    async def commit_task(self, request: CommitRequest) -> CommitResult:
        """Execute the commit workflow for a single task.

        Enforces verification gate, handles approval gate, performs commit,
        and emits appropriate events.

        Args:
            request: The commit request with task details.

        Returns:
            CommitResult with the outcome.

        Raises:
            CommitNotVerifiedError: If verify.passed has not been emitted
                for this task (Req 9.2).
        """
        task_id = request.task_id

        # Req 9.2: Never commit without verify.passed
        if not self.is_verified(task_id):
            result = CommitResult(
                task_id=task_id,
                status=CommitStatus.PENDING_VERIFICATION,
                error=f"Task '{task_id}' has not passed verification",
            )
            self._commit_results[task_id] = result
            raise CommitNotVerifiedError(task_id)

        # Req 9.4: Approval gate (if configured)
        if request.approval_required:
            approved = await self._handle_approval_gate(request)
            if not approved:
                result = CommitResult(
                    task_id=task_id,
                    status=CommitStatus.REJECTED,
                    error="Commit was not approved",
                )
                self._commit_results[task_id] = result
                return result

        # Perform the commit (Req 9.1)
        result = await self._perform_commit(request)
        self._commit_results[task_id] = result
        return result

    async def _handle_approval_gate(self, request: CommitRequest) -> bool:
        """Handle the approval gate for a commit.

        Pauses execution, requests approval, and emits an approval.pending event.

        Args:
            request: The commit request requiring approval.

        Returns:
            True if approved, False if rejected or timed out.
        """
        task_id = request.task_id

        # Emit approval.pending event (Req 9.4)
        await self._emit_approval_pending(request)

        # Record the approval gate decision
        record = DecisionRecord(
            kind=DecisionKind.APPROVAL_GATE,
            subject=task_id,
            inputs={
                "task_id": task_id,
                "action": "commit",
                "changed_files": request.changed_files,
                "session_id": request.session_id,
            },
            decision="pending",
            rationale=(
                f"Commit for task '{task_id}' requires approval. "
                f"Action paused pending explicit user approval."
            ),
            alternatives=["auto_commit", "reject"],
            session_id=request.session_id,
        )
        self._decisions.append(record)

        # Request approval from provider
        if self._approval_provider is None:
            # No approval provider available — reject by default
            await self._emit_approval_rejected(request, reason="no_approval_provider")
            return False

        try:
            approved = await self._approval_provider.request_approval(
                action="commit",
                task_id=task_id,
                session_id=request.session_id,
                details={
                    "changed_files": request.changed_files,
                    "commit_message": request.commit_message,
                    "workspace_path": request.workspace_path,
                },
            )
        except Exception as exc:
            logger.exception(
                "Approval request failed for task %s: %s", task_id, exc
            )
            await self._emit_approval_rejected(
                request, reason=f"approval_error: {exc}"
            )
            return False

        if not approved:
            await self._emit_approval_rejected(request, reason="user_rejected")
            return False

        return True

    async def _perform_commit(self, request: CommitRequest) -> CommitResult:
        """Perform the actual commit operation.

        On success: emits commit.done with SHA and changed files.
        On failure: leaves repo unchanged, emits error event.

        Args:
            request: The commit request.

        Returns:
            CommitResult with success or failure.
        """
        task_id = request.task_id

        try:
            # Perform the VCS commit (Req 9.1)
            commit_sha = await self._vcs_committer.commit(
                repo_path=self._canonical_repo_path,
                message=request.commit_message or f"forge: task {task_id}",
                files=request.changed_files,
            )
        except Exception as exc:
            # Req 9.5: On failure, leave repo unchanged and emit error
            error_msg = f"Commit operation failed: {exc}"
            logger.error("Commit failed for task %s: %s", task_id, exc)

            await self._emit_commit_error(request, error_msg)

            return CommitResult(
                task_id=task_id,
                status=CommitStatus.FAILED,
                error=error_msg,
            )

        # Req 9.1: Emit commit.done with SHA and changed files
        await self._emit_commit_done(task_id, commit_sha, request.changed_files)

        return CommitResult(
            task_id=task_id,
            status=CommitStatus.COMMITTED,
            commit_sha=commit_sha,
            changed_files=request.changed_files,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Event emission helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _emit_commit_done(
        self, task_id: str, commit_sha: str, changed_files: list[str]
    ) -> None:
        """Emit a commit.done event (Req 9.1).

        Carries: commit SHA and list of changed file paths.
        """
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.COMMIT_DONE,
            session_id=self._session_id,
            source="commit_workflow",
            payload={
                "task_id": task_id,
                "commit_sha": commit_sha,
                "changed_files": changed_files,
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit commit.done event for task %s", task_id
            )

    async def _emit_commit_error(
        self, request: CommitRequest, error_msg: str
    ) -> None:
        """Emit an error event indicating commit failure (Req 9.5).

        Carries: commit failure indication and affected task identifier.
        """
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.COMMIT_FAILED,
            session_id=self._session_id,
            source="commit_workflow",
            payload={
                "task_id": request.task_id,
                "error": error_msg,
                "changed_files": request.changed_files,
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit commit.failed event for task %s", request.task_id
            )

    async def _emit_approval_pending(self, request: CommitRequest) -> None:
        """Emit an approval.pending event (Req 9.4).

        Indicates that approval is required before the commit proceeds.
        """
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.APPROVAL_PENDING,
            session_id=self._session_id,
            source="commit_workflow",
            payload={
                "task_id": request.task_id,
                "action": "commit",
                "changed_files": request.changed_files,
                "commit_message": request.commit_message,
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit approval.pending event for task %s", request.task_id
            )

    async def _emit_approval_rejected(
        self, request: CommitRequest, reason: str
    ) -> None:
        """Emit an approval.rejected event (Req 9.7).

        Indicates that the approval-gated action was not approved.
        """
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.APPROVAL_REJECTED,
            session_id=self._session_id,
            source="commit_workflow",
            payload={
                "task_id": request.task_id,
                "action": "commit",
                "reason": reason,
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit approval.rejected event for task %s",
                request.task_id,
            )
