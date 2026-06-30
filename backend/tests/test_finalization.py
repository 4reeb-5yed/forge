"""Unit tests for the FinalizationWorkflow component.

Tests cover:
- Push canonical repo to VCS after all tasks processed (Req 9.3)
- Emit build.done with summary, committed tasks, and skipped tasks (Req 9.3)
- Handle push failure: retain commits, emit error, no build.done (Req 9.6)
- Handle approval timeout/rejection: do not perform action, emit event (Req 9.7)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime.events.models import DecisionKind, Event, EventType
from app.runtime.finalization import (
    ApprovalRequest,
    ApprovalStatus,
    BuildSummary,
    DEFAULT_APPROVAL_TIMEOUT,
    FinalizationWorkflow,
    PushFailureError,
)


# ─── Test Helpers ───────────────────────────────────────────────────────────


class FakeEventPublisher:
    """Captures published events for assertion."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


class FakeVCSPusher:
    """VCS pusher that can be configured to succeed or fail."""

    def __init__(self, should_fail: bool = False, error_message: str = "push error") -> None:
        self.should_fail = should_fail
        self.error_message = error_message
        self.push_called = False
        self.push_repo_path: str | None = None

    async def push(self, repo_path: str) -> None:
        self.push_called = True
        self.push_repo_path = repo_path
        if self.should_fail:
            raise RuntimeError(self.error_message)


class FakeAuditWriter:
    """Captures written decisions for assertion."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []

    def write_decision(self, **kwargs: Any) -> None:
        self.decisions.append(kwargs)


def _make_workflow(
    vcs_fails: bool = False,
    push_requires_approval: bool = False,
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT,
    error_message: str = "push error",
) -> tuple[FinalizationWorkflow, FakeEventPublisher, FakeVCSPusher, FakeAuditWriter]:
    """Create a FinalizationWorkflow with fake dependencies."""
    publisher = FakeEventPublisher()
    pusher = FakeVCSPusher(should_fail=vcs_fails, error_message=error_message)
    audit = FakeAuditWriter()

    workflow = FinalizationWorkflow(
        event_publisher=publisher,
        vcs_pusher=pusher,
        audit_writer=audit,
        session_id="session-123",
        canonical_path="/repo/canonical",
        approval_timeout=approval_timeout,
        push_requires_approval=push_requires_approval,
    )
    return workflow, publisher, pusher, audit


# ─── Tests: Successful Push and build.done (Req 9.3) ───────────────────────


class TestSuccessfulFinalization:
    """Tests for successful push to VCS and build.done emission."""

    @pytest.mark.asyncio
    async def test_push_called_with_canonical_path(self) -> None:
        """Push is called with the canonical repository path."""
        workflow, publisher, pusher, audit = _make_workflow()

        await workflow.finalize(
            committed_tasks=["task-1", "task-2"],
            skipped_tasks=["task-3"],
            failed_tasks=[],
        )

        assert pusher.push_called is True
        assert pusher.push_repo_path == "/repo/canonical"

    @pytest.mark.asyncio
    async def test_build_done_emitted_on_success(self) -> None:
        """build.done event is emitted after successful push."""
        workflow, publisher, pusher, audit = _make_workflow()

        await workflow.finalize(
            committed_tasks=["task-1", "task-2"],
            skipped_tasks=["task-3"],
            failed_tasks=[],
        )

        done_events = publisher.events_of_type(EventType.BUILD_DONE)
        assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_build_done_carries_committed_tasks(self) -> None:
        """build.done payload includes committed task IDs."""
        workflow, publisher, pusher, audit = _make_workflow()

        await workflow.finalize(
            committed_tasks=["task-1", "task-2"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        done_event = publisher.events_of_type(EventType.BUILD_DONE)[0]
        assert done_event.payload["committed"] == ["task-1", "task-2"]

    @pytest.mark.asyncio
    async def test_build_done_carries_skipped_tasks(self) -> None:
        """build.done payload includes skipped task IDs."""
        workflow, publisher, pusher, audit = _make_workflow()

        await workflow.finalize(
            committed_tasks=["task-1"],
            skipped_tasks=["task-2", "task-3"],
            failed_tasks=[],
        )

        done_event = publisher.events_of_type(EventType.BUILD_DONE)[0]
        assert done_event.payload["skipped"] == ["task-2", "task-3"]

    @pytest.mark.asyncio
    async def test_build_done_carries_summary(self) -> None:
        """build.done payload includes a human-readable summary."""
        workflow, publisher, pusher, audit = _make_workflow()

        await workflow.finalize(
            committed_tasks=["task-1"],
            skipped_tasks=["task-2"],
            failed_tasks=["task-3"],
        )

        done_event = publisher.events_of_type(EventType.BUILD_DONE)[0]
        assert "summary" in done_event.payload
        assert "1 committed" in done_event.payload["summary"]
        assert "1 skipped" in done_event.payload["summary"]
        assert "1 failed" in done_event.payload["summary"]

    @pytest.mark.asyncio
    async def test_build_done_carries_total_tasks(self) -> None:
        """build.done payload includes total task count."""
        workflow, publisher, pusher, audit = _make_workflow()

        await workflow.finalize(
            committed_tasks=["t1", "t2"],
            skipped_tasks=["t3"],
            failed_tasks=[],
            total_tasks=3,
        )

        done_event = publisher.events_of_type(EventType.BUILD_DONE)[0]
        assert done_event.payload["total_tasks"] == 3

    @pytest.mark.asyncio
    async def test_returns_build_summary(self) -> None:
        """Successful finalization returns a BuildSummary."""
        workflow, publisher, pusher, audit = _make_workflow()

        result = await workflow.finalize(
            committed_tasks=["t1", "t2"],
            skipped_tasks=["t3"],
            failed_tasks=["t4"],
        )

        assert result is not None
        assert result.committed_tasks == ["t1", "t2"]
        assert result.skipped_tasks == ["t3"]
        assert result.failed_tasks == ["t4"]
        assert result.total_tasks == 4
        assert result.session_id == "session-123"

    @pytest.mark.asyncio
    async def test_build_done_event_source(self) -> None:
        """build.done event has correct source and session_id."""
        workflow, publisher, pusher, audit = _make_workflow()

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        done_event = publisher.events_of_type(EventType.BUILD_DONE)[0]
        assert done_event.source == "finalization_workflow"
        assert done_event.session_id == "session-123"


# ─── Tests: Push Failure (Req 9.6) ─────────────────────────────────────────


class TestPushFailure:
    """Tests for push failure handling."""

    @pytest.mark.asyncio
    async def test_no_build_done_on_push_failure(self) -> None:
        """build.done is NOT emitted when push fails."""
        workflow, publisher, pusher, audit = _make_workflow(vcs_fails=True)

        result = await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        assert result is None
        done_events = publisher.events_of_type(EventType.BUILD_DONE)
        assert len(done_events) == 0

    @pytest.mark.asyncio
    async def test_error_event_emitted_on_push_failure(self) -> None:
        """An error event is emitted indicating push failure."""
        workflow, publisher, pusher, audit = _make_workflow(
            vcs_fails=True, error_message="network timeout"
        )

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        error_events = publisher.events_of_type(EventType.ERROR)
        assert len(error_events) == 1
        assert error_events[0].payload["error_type"] == "push_failure"
        assert "network timeout" in error_events[0].payload["message"]

    @pytest.mark.asyncio
    async def test_error_event_indicates_recoverable(self) -> None:
        """Push failure error event marks as recoverable."""
        workflow, publisher, pusher, audit = _make_workflow(vcs_fails=True)

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        error_event = publisher.events_of_type(EventType.ERROR)[0]
        assert error_event.payload["recoverable"] is True

    @pytest.mark.asyncio
    async def test_commits_retained_on_push_failure(self) -> None:
        """Commits are retained in the canonical repo on push failure.

        This is implicit — the workflow does NOT modify the repo on failure.
        We verify by confirming no destructive action is taken.
        """
        workflow, publisher, pusher, audit = _make_workflow(vcs_fails=True)

        result = await workflow.finalize(
            committed_tasks=["t1", "t2"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        # Push was attempted
        assert pusher.push_called is True
        # No build.done emitted (commits retained, not reverted)
        assert result is None

    @pytest.mark.asyncio
    async def test_decision_recorded_on_push_failure(self) -> None:
        """A DecisionRecord is written when push fails."""
        workflow, publisher, pusher, audit = _make_workflow(vcs_fails=True)

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        assert len(audit.decisions) == 1
        decision = audit.decisions[0]
        assert decision["kind"] == DecisionKind.TASK_OUTCOME
        assert decision["subject"] == "push_to_vcs"
        assert decision["decision"] == "push_failed"
        assert decision["session_id"] == "session-123"

    @pytest.mark.asyncio
    async def test_returns_none_on_push_failure(self) -> None:
        """Finalization returns None on push failure."""
        workflow, publisher, pusher, audit = _make_workflow(vcs_fails=True)

        result = await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        assert result is None


# ─── Tests: Approval Gate (Req 9.7) ────────────────────────────────────────


class TestApprovalGate:
    """Tests for approval-gated push behavior."""

    @pytest.mark.asyncio
    async def test_approval_pending_event_emitted(self) -> None:
        """When push requires approval, an approval pending event is emitted."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=0.1,  # short timeout for testing
        )

        # Run finalize — will timeout since nobody approves
        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        question_events = publisher.events_of_type(EventType.QUESTION)
        assert len(question_events) == 1
        assert question_events[0].payload["type"] == "approval_pending"
        assert question_events[0].payload["action"] == "push_to_vcs"

    @pytest.mark.asyncio
    async def test_no_push_on_approval_timeout(self) -> None:
        """Push is NOT performed when approval times out."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=0.05,  # very short timeout
        )

        result = await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        assert pusher.push_called is False
        assert result is None

    @pytest.mark.asyncio
    async def test_no_push_on_approval_rejection(self) -> None:
        """Push is NOT performed when approval is rejected."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=5.0,
        )

        async def reject_after_short_delay() -> None:
            # Wait for the approval request to be created
            await asyncio.sleep(0.01)
            for req_id, req in workflow.pending_approvals.items():
                workflow.respond_to_approval(req_id, approved=False)

        # Run finalization and rejection concurrently
        result, _ = await asyncio.gather(
            workflow.finalize(
                committed_tasks=["t1"],
                skipped_tasks=[],
                failed_tasks=[],
            ),
            reject_after_short_delay(),
        )

        assert pusher.push_called is False
        assert result is None

    @pytest.mark.asyncio
    async def test_push_proceeds_on_approval(self) -> None:
        """Push proceeds when approval is granted."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=5.0,
        )

        async def approve_after_short_delay() -> None:
            await asyncio.sleep(0.01)
            for req_id, req in workflow.pending_approvals.items():
                workflow.respond_to_approval(req_id, approved=True)

        result, _ = await asyncio.gather(
            workflow.finalize(
                committed_tasks=["t1"],
                skipped_tasks=[],
                failed_tasks=[],
            ),
            approve_after_short_delay(),
        )

        assert pusher.push_called is True
        assert result is not None
        assert result.committed_tasks == ["t1"]

    @pytest.mark.asyncio
    async def test_not_approved_event_on_timeout(self) -> None:
        """An event indicating action not approved is emitted on timeout."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=0.05,
        )

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        error_events = publisher.events_of_type(EventType.ERROR)
        assert len(error_events) == 1
        assert error_events[0].payload["error_type"] == "approval_not_granted"
        assert error_events[0].payload["status"] == "timed_out"

    @pytest.mark.asyncio
    async def test_not_approved_event_on_rejection(self) -> None:
        """An event indicating action not approved is emitted on rejection."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=5.0,
        )

        async def reject_after_short_delay() -> None:
            await asyncio.sleep(0.01)
            for req_id, req in workflow.pending_approvals.items():
                workflow.respond_to_approval(req_id, approved=False)

        await asyncio.gather(
            workflow.finalize(
                committed_tasks=["t1"],
                skipped_tasks=[],
                failed_tasks=[],
            ),
            reject_after_short_delay(),
        )

        error_events = publisher.events_of_type(EventType.ERROR)
        assert len(error_events) == 1
        assert error_events[0].payload["error_type"] == "approval_not_granted"
        assert error_events[0].payload["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_no_build_done_on_approval_timeout(self) -> None:
        """build.done is NOT emitted when approval times out."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=0.05,
        )

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        done_events = publisher.events_of_type(EventType.BUILD_DONE)
        assert len(done_events) == 0

    @pytest.mark.asyncio
    async def test_decision_recorded_for_approval_request(self) -> None:
        """DecisionRecord is written when approval is requested."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=0.05,
        )

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        # Should have at least 2 decisions: request + outcome
        assert len(audit.decisions) >= 2
        # First decision is the approval request
        assert audit.decisions[0]["kind"] == DecisionKind.APPROVAL_GATE
        assert audit.decisions[0]["decision"] == "approval_requested"

    @pytest.mark.asyncio
    async def test_decision_recorded_for_approval_timeout(self) -> None:
        """DecisionRecord captures timeout status."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=0.05,
        )

        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        # Last decision records the timeout
        timeout_decisions = [
            d for d in audit.decisions
            if "timed_out" in d.get("decision", "")
        ]
        assert len(timeout_decisions) == 1


# ─── Tests: ApprovalRequest ────────────────────────────────────────────────


class TestApprovalRequest:
    """Tests for the ApprovalRequest dataclass."""

    @pytest.mark.asyncio
    async def test_approve_sets_status(self) -> None:
        """Calling approve() sets status to APPROVED."""
        req = ApprovalRequest(
            request_id="req-1", action="push", session_id="s1"
        )
        req.approve()
        assert req.status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_reject_sets_status(self) -> None:
        """Calling reject() sets status to REJECTED."""
        req = ApprovalRequest(
            request_id="req-1", action="push", session_id="s1"
        )
        req.reject()
        assert req.status == ApprovalStatus.REJECTED

    @pytest.mark.asyncio
    async def test_wait_returns_on_approve(self) -> None:
        """wait() resolves when approved."""
        req = ApprovalRequest(
            request_id="req-1", action="push", session_id="s1"
        )

        async def approve_soon() -> None:
            await asyncio.sleep(0.01)
            req.approve()

        asyncio.create_task(approve_soon())
        status = await req.wait(timeout=1.0)
        assert status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_wait_returns_timed_out(self) -> None:
        """wait() returns TIMED_OUT when timeout elapses."""
        req = ApprovalRequest(
            request_id="req-1", action="push", session_id="s1"
        )
        status = await req.wait(timeout=0.01)
        assert status == ApprovalStatus.TIMED_OUT


# ─── Tests: BuildSummary ───────────────────────────────────────────────────


class TestBuildSummary:
    """Tests for the BuildSummary dataclass."""

    def test_summary_text_all_committed(self) -> None:
        """Summary text correctly reports all committed."""
        summary = BuildSummary(
            session_id="s1",
            total_tasks=3,
            committed_tasks=["t1", "t2", "t3"],
            skipped_tasks=[],
            failed_tasks=[],
        )
        assert "3 committed" in summary.summary_text
        assert "0 skipped" in summary.summary_text
        assert "0 failed" in summary.summary_text

    def test_summary_text_mixed(self) -> None:
        """Summary text correctly reports mixed outcomes."""
        summary = BuildSummary(
            session_id="s1",
            total_tasks=5,
            committed_tasks=["t1", "t2"],
            skipped_tasks=["t3", "t4"],
            failed_tasks=["t5"],
        )
        assert "2 committed" in summary.summary_text
        assert "2 skipped" in summary.summary_text
        assert "1 failed" in summary.summary_text
        assert "5 total tasks" in summary.summary_text


# ─── Tests: respond_to_approval ────────────────────────────────────────────


class TestRespondToApproval:
    """Tests for the respond_to_approval method."""

    def test_respond_to_nonexistent_returns_false(self) -> None:
        """Responding to unknown request_id returns False."""
        workflow, _, _, _ = _make_workflow()
        assert workflow.respond_to_approval("nonexistent", approved=True) is False

    @pytest.mark.asyncio
    async def test_respond_to_already_resolved_returns_false(self) -> None:
        """Responding to already-resolved request returns False."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=True,
            approval_timeout=0.05,
        )

        # Let it timeout
        await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        # All pending approvals should be cleaned up
        assert workflow.respond_to_approval("any-id", approved=True) is False


# ─── Tests: No approval needed (default path) ──────────────────────────────


class TestNoApprovalRequired:
    """Tests when push does not require approval (default)."""

    @pytest.mark.asyncio
    async def test_push_proceeds_immediately(self) -> None:
        """Without approval gate, push proceeds immediately."""
        workflow, publisher, pusher, audit = _make_workflow(
            push_requires_approval=False,
        )

        result = await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        assert pusher.push_called is True
        assert result is not None
        # No question/approval events
        question_events = publisher.events_of_type(EventType.QUESTION)
        assert len(question_events) == 0

    @pytest.mark.asyncio
    async def test_empty_committed_list(self) -> None:
        """Finalization works with empty committed list (all skipped/failed)."""
        workflow, publisher, pusher, audit = _make_workflow()

        result = await workflow.finalize(
            committed_tasks=[],
            skipped_tasks=["t1", "t2"],
            failed_tasks=["t3"],
        )

        assert result is not None
        assert result.committed_tasks == []
        assert result.skipped_tasks == ["t1", "t2"]
        done_event = publisher.events_of_type(EventType.BUILD_DONE)[0]
        assert done_event.payload["committed"] == []
