"""Unit tests for the CommitWorkflow module.

Tests commit on verify.passed, rejection without it, approval gate
pause/approval/rejection, and commit failure handling.

Requirements: 9.1, 9.2, 9.4, 9.5
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime.commit import (
    ApprovalProvider,
    CommitFailedError,
    CommitNotVerifiedError,
    CommitRequest,
    CommitResult,
    CommitStatus,
    CommitWorkflow,
    VCSCommitter,
)
from app.runtime.events.models import Event, EventType


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────


class FakeVCSCommitter:
    """Fake VCS committer for testing."""

    def __init__(self, commit_sha: str = "abc123def456") -> None:
        self.commit_sha = commit_sha
        self.calls: list[dict[str, Any]] = []
        self.should_fail = False
        self.fail_error = "git commit failed"

    async def commit(self, repo_path: str, message: str, files: list[str]) -> str:
        self.calls.append({
            "repo_path": repo_path,
            "message": message,
            "files": files,
        })
        if self.should_fail:
            raise RuntimeError(self.fail_error)
        return self.commit_sha


class FakeApprovalProvider:
    """Fake approval provider for testing."""

    def __init__(self, auto_approve: bool = True) -> None:
        self.auto_approve = auto_approve
        self.requests: list[dict[str, Any]] = []
        self.should_error = False

    async def request_approval(
        self, action: str, task_id: str, session_id: str, details: dict[str, Any]
    ) -> bool:
        self.requests.append({
            "action": action,
            "task_id": task_id,
            "session_id": session_id,
            "details": details,
        })
        if self.should_error:
            raise RuntimeError("Approval service unavailable")
        return self.auto_approve


class EventCollector:
    """Collects emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def get_events_by_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


@pytest.fixture
def vcs_committer() -> FakeVCSCommitter:
    return FakeVCSCommitter()


@pytest.fixture
def approval_provider() -> FakeApprovalProvider:
    return FakeApprovalProvider()


@pytest.fixture
def event_collector() -> EventCollector:
    return EventCollector()


@pytest.fixture
def commit_workflow(
    vcs_committer: FakeVCSCommitter,
    event_collector: EventCollector,
) -> CommitWorkflow:
    return CommitWorkflow(
        vcs_committer=vcs_committer,
        event_emitter=event_collector.emit,
        session_id="session-1",
        canonical_repo_path="/repos/canonical",
    )


@pytest.fixture
def commit_request() -> CommitRequest:
    return CommitRequest(
        task_id="task-1",
        session_id="session-1",
        workspace_path="/workspaces/task-1",
        changed_files=["src/main.py", "src/utils.py"],
        commit_message="feat: implement feature X",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test: Commit on verify.passed (Req 9.1)
# ─────────────────────────────────────────────────────────────────────────────


class TestCommitOnVerifyPassed:
    """Tests for committing after verify.passed (Req 9.1)."""

    @pytest.mark.asyncio
    async def test_commit_emits_commit_done_with_sha_and_files(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
        commit_request: CommitRequest,
    ) -> None:
        """Req 9.1: Commit on verify.passed emits commit.done with SHA and files."""
        commit_workflow.mark_verified("task-1")

        result = await commit_workflow.commit_task(commit_request)

        assert result.success is True
        assert result.status == CommitStatus.COMMITTED
        assert result.commit_sha == "abc123def456"
        assert result.changed_files == ["src/main.py", "src/utils.py"]

        # Verify commit.done event was emitted
        done_events = event_collector.get_events_by_type(EventType.COMMIT_DONE)
        assert len(done_events) == 1
        payload = done_events[0].payload
        assert payload["task_id"] == "task-1"
        assert payload["commit_sha"] == "abc123def456"
        assert payload["changed_files"] == ["src/main.py", "src/utils.py"]

    @pytest.mark.asyncio
    async def test_commit_passes_correct_args_to_vcs(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
        commit_request: CommitRequest,
    ) -> None:
        """Req 9.1: VCS committer receives correct repo path, message, and files."""
        commit_workflow.mark_verified("task-1")

        await commit_workflow.commit_task(commit_request)

        assert len(vcs_committer.calls) == 1
        call = vcs_committer.calls[0]
        assert call["repo_path"] == "/repos/canonical"
        assert call["message"] == "feat: implement feature X"
        assert call["files"] == ["src/main.py", "src/utils.py"]

    @pytest.mark.asyncio
    async def test_default_commit_message_when_not_provided(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
    ) -> None:
        """Req 9.1: Default commit message includes task ID."""
        commit_workflow.mark_verified("task-1")
        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
        )

        await commit_workflow.commit_task(request)

        call = vcs_committer.calls[0]
        assert "task-1" in call["message"]


# ─────────────────────────────────────────────────────────────────────────────
# Test: Never commit without verify.passed (Req 9.2)
# ─────────────────────────────────────────────────────────────────────────────


class TestRejectWithoutVerification:
    """Tests for rejecting commits without verify.passed (Req 9.2)."""

    @pytest.mark.asyncio
    async def test_commit_raises_without_verification(
        self,
        commit_workflow: CommitWorkflow,
        commit_request: CommitRequest,
    ) -> None:
        """Req 9.2: Raise CommitNotVerifiedError if verify.passed not emitted."""
        with pytest.raises(CommitNotVerifiedError) as exc_info:
            await commit_workflow.commit_task(commit_request)

        assert exc_info.value.task_id == "task-1"

    @pytest.mark.asyncio
    async def test_vcs_not_called_without_verification(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
        commit_request: CommitRequest,
    ) -> None:
        """Req 9.2: VCS committer is never called if not verified."""
        with pytest.raises(CommitNotVerifiedError):
            await commit_workflow.commit_task(commit_request)

        assert len(vcs_committer.calls) == 0

    @pytest.mark.asyncio
    async def test_is_verified_returns_false_for_unknown_task(
        self,
        commit_workflow: CommitWorkflow,
    ) -> None:
        """Req 9.2: Task not in verified set returns False."""
        assert commit_workflow.is_verified("task-1") is False

    @pytest.mark.asyncio
    async def test_is_verified_returns_true_after_marking(
        self,
        commit_workflow: CommitWorkflow,
    ) -> None:
        """Req 9.2: Task returns True after mark_verified."""
        commit_workflow.mark_verified("task-1")
        assert commit_workflow.is_verified("task-1") is True


# ─────────────────────────────────────────────────────────────────────────────
# Test: Approval-gated actions (Req 9.4)
# ─────────────────────────────────────────────────────────────────────────────


class TestApprovalGate:
    """Tests for approval-gated commit actions (Req 9.4)."""

    @pytest.mark.asyncio
    async def test_approval_gate_emits_pending_event(
        self,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.4: Approval-gated action emits approval.pending event."""
        provider = FakeApprovalProvider(auto_approve=True)
        workflow = CommitWorkflow(
            vcs_committer=vcs_committer,
            event_emitter=event_collector.emit,
            approval_provider=provider,
            session_id="session-1",
            canonical_repo_path="/repos/canonical",
        )
        workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
            approval_required=True,
        )

        await workflow.commit_task(request)

        pending_events = event_collector.get_events_by_type(EventType.APPROVAL_REQUESTED)
        assert len(pending_events) == 1
        payload = pending_events[0].payload
        assert payload["task_id"] == "task-1"
        assert payload["action"] == "commit"

    @pytest.mark.asyncio
    async def test_approved_commit_proceeds(
        self,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.4: Approved commit proceeds and produces commit.done."""
        provider = FakeApprovalProvider(auto_approve=True)
        workflow = CommitWorkflow(
            vcs_committer=vcs_committer,
            event_emitter=event_collector.emit,
            approval_provider=provider,
            session_id="session-1",
            canonical_repo_path="/repos/canonical",
        )
        workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
            approval_required=True,
        )

        result = await workflow.commit_task(request)

        assert result.success is True
        assert result.commit_sha == "abc123def456"
        done_events = event_collector.get_events_by_type(EventType.COMMIT_DONE)
        assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_rejected_approval_does_not_commit(
        self,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.4/9.7: Rejected approval does not commit, emits rejected event."""
        provider = FakeApprovalProvider(auto_approve=False)
        workflow = CommitWorkflow(
            vcs_committer=vcs_committer,
            event_emitter=event_collector.emit,
            approval_provider=provider,
            session_id="session-1",
            canonical_repo_path="/repos/canonical",
        )
        workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
            approval_required=True,
        )

        result = await workflow.commit_task(request)

        assert result.success is False
        assert result.status == CommitStatus.REJECTED
        assert len(vcs_committer.calls) == 0

        rejected_events = event_collector.get_events_by_type(EventType.APPROVAL_REJECTED)
        assert len(rejected_events) == 1
        assert rejected_events[0].payload["reason"] == "user_rejected"

    @pytest.mark.asyncio
    async def test_no_approval_provider_rejects_gated_commit(
        self,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.4: Without approval provider, gated commits are rejected."""
        workflow = CommitWorkflow(
            vcs_committer=vcs_committer,
            event_emitter=event_collector.emit,
            approval_provider=None,
            session_id="session-1",
            canonical_repo_path="/repos/canonical",
        )
        workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
            approval_required=True,
        )

        result = await workflow.commit_task(request)

        assert result.success is False
        assert result.status == CommitStatus.REJECTED
        assert len(vcs_committer.calls) == 0

    @pytest.mark.asyncio
    async def test_approval_provider_error_rejects(
        self,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.4: Approval provider error results in rejection."""
        provider = FakeApprovalProvider(auto_approve=True)
        provider.should_error = True
        workflow = CommitWorkflow(
            vcs_committer=vcs_committer,
            event_emitter=event_collector.emit,
            approval_provider=provider,
            session_id="session-1",
            canonical_repo_path="/repos/canonical",
        )
        workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
            approval_required=True,
        )

        result = await workflow.commit_task(request)

        assert result.success is False
        assert result.status == CommitStatus.REJECTED

    @pytest.mark.asyncio
    async def test_approval_gate_records_decision(
        self,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.4: Approval gate records a DecisionRecord."""
        provider = FakeApprovalProvider(auto_approve=True)
        workflow = CommitWorkflow(
            vcs_committer=vcs_committer,
            event_emitter=event_collector.emit,
            approval_provider=provider,
            session_id="session-1",
            canonical_repo_path="/repos/canonical",
        )
        workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
            approval_required=True,
        )

        await workflow.commit_task(request)

        decisions = workflow.decisions
        assert len(decisions) == 1
        assert decisions[0].subject == "task-1"
        assert decisions[0].inputs["action"] == "commit"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Commit failure handling (Req 9.5)
# ─────────────────────────────────────────────────────────────────────────────


class TestCommitFailure:
    """Tests for commit failure handling (Req 9.5)."""

    @pytest.mark.asyncio
    async def test_commit_failure_emits_error_event(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.5: Commit failure emits error event with task ID."""
        vcs_committer.should_fail = True
        commit_workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
        )

        result = await commit_workflow.commit_task(request)

        assert result.success is False
        assert result.status == CommitStatus.FAILED
        assert "failed" in result.error.lower()

        # Verify error event emitted
        error_events = event_collector.get_events_by_type(EventType.COMMIT_FAILED)
        assert len(error_events) == 1
        payload = error_events[0].payload
        assert payload["task_id"] == "task-1"
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_commit_failure_does_not_emit_commit_done(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Req 9.5: On failure, commit.done is NOT emitted."""
        vcs_committer.should_fail = True
        commit_workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
        )

        await commit_workflow.commit_task(request)

        done_events = event_collector.get_events_by_type(EventType.COMMIT_DONE)
        assert len(done_events) == 0

    @pytest.mark.asyncio
    async def test_commit_failure_result_has_no_sha(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
    ) -> None:
        """Req 9.5: Failed commit has empty SHA (repo left unchanged)."""
        vcs_committer.should_fail = True
        commit_workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
        )

        result = await commit_workflow.commit_task(request)

        assert result.commit_sha == ""
        assert result.changed_files == []


# ─────────────────────────────────────────────────────────────────────────────
# Test: Non-gated commits (no approval required)
# ─────────────────────────────────────────────────────────────────────────────


class TestNonGatedCommit:
    """Tests for commits that do not require approval."""

    @pytest.mark.asyncio
    async def test_non_gated_commit_skips_approval(
        self,
        commit_workflow: CommitWorkflow,
        event_collector: EventCollector,
    ) -> None:
        """Non-gated commits do not emit approval.pending."""
        commit_workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
            approval_required=False,
        )

        result = await commit_workflow.commit_task(request)

        assert result.success is True
        pending_events = event_collector.get_events_by_type(EventType.APPROVAL_REQUESTED)
        assert len(pending_events) == 0

    @pytest.mark.asyncio
    async def test_multiple_tasks_committed_independently(
        self,
        commit_workflow: CommitWorkflow,
        vcs_committer: FakeVCSCommitter,
        event_collector: EventCollector,
    ) -> None:
        """Multiple verified tasks can each be committed."""
        commit_workflow.mark_verified("task-1")
        commit_workflow.mark_verified("task-2")

        request1 = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["a.py"],
        )
        request2 = CommitRequest(
            task_id="task-2",
            session_id="session-1",
            workspace_path="/workspaces/task-2",
            changed_files=["b.py"],
        )

        result1 = await commit_workflow.commit_task(request1)
        result2 = await commit_workflow.commit_task(request2)

        assert result1.success is True
        assert result2.success is True
        assert len(vcs_committer.calls) == 2

    @pytest.mark.asyncio
    async def test_commit_results_tracked(
        self,
        commit_workflow: CommitWorkflow,
    ) -> None:
        """Commit results are tracked per task."""
        commit_workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
        )

        await commit_workflow.commit_task(request)

        results = commit_workflow.commit_results
        assert "task-1" in results
        assert results["task-1"].success is True


# ─────────────────────────────────────────────────────────────────────────────
# Test: Event emission without emitter
# ─────────────────────────────────────────────────────────────────────────────


class TestNoEventEmitter:
    """Tests that workflow works correctly without an event emitter."""

    @pytest.mark.asyncio
    async def test_commit_succeeds_without_emitter(
        self,
        vcs_committer: FakeVCSCommitter,
    ) -> None:
        """Commit proceeds even when no event emitter is configured."""
        workflow = CommitWorkflow(
            vcs_committer=vcs_committer,
            event_emitter=None,
            session_id="session-1",
            canonical_repo_path="/repos/canonical",
        )
        workflow.mark_verified("task-1")

        request = CommitRequest(
            task_id="task-1",
            session_id="session-1",
            workspace_path="/workspaces/task-1",
            changed_files=["file.py"],
        )

        result = await workflow.commit_task(request)

        assert result.success is True
        assert result.commit_sha == "abc123def456"
