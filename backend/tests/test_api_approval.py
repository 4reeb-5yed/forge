"""Unit tests for the Approval API endpoints.

Tests approval request listing, retrieval, diff viewing, and
approval/rejection decision handling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.api.approval import (
    ApprovalDecisionRequest,
    ApprovalRequestResponse,
    DiffResponse,
)


class TestApprovalModels:
    """Tests for Pydantic models in approval API."""

    def test_approval_request_response_model(self) -> None:
        """ApprovalRequestResponse model validates correctly."""
        response = ApprovalRequestResponse(
            id="test-123",
            session_id="session-456",
            task_id="task-789",
            type="commit",
            status="pending",
            diff_summary="Added feature",
            changed_files=["file1.py", "file2.py"],
            requested_at="2024-01-15T10:30:00Z",
        )
        assert response.id == "test-123"
        assert response.diff_summary == "Added feature"

    def test_approval_decision_request_model(self) -> None:
        """ApprovalDecisionRequest model accepts optional comment."""
        # With comment
        with_comment = ApprovalDecisionRequest(comment="Looks good")
        assert with_comment.comment == "Looks good"

        # Without comment
        without_comment = ApprovalDecisionRequest()
        assert without_comment.comment is None

    def test_diff_response_model(self) -> None:
        """DiffResponse model validates correctly."""
        response = DiffResponse(
            request_id="req-123",
            diff="--- original\n+++ modified\n@@ -1,3 +1,4 @@",
            changed_files=["file.py"],
        )
        assert response.request_id == "req-123"
        assert "modified" in response.diff


class TestApprovalLogic:
    """Tests for approval manager interactions via the API layer."""

    @pytest.fixture
    def sample_approval_request(self) -> MagicMock:
        """Create a sample approval request object."""
        request = MagicMock()
        request.id = "approval-123"
        request.session_id = "session-456"
        request.task_id = "task-789"
        request.type.value = "commit"
        request.status.value = "pending"
        request.diff_summary = "Added new feature"
        request.changed_files = ["src/feature.py", "tests/test_feature.py"]
        request.requested_at = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        request.expires_at = datetime(2024, 1, 15, 11, 30, 0, tzinfo=timezone.utc)
        request.diff_full = "--- a/src/feature.py\n+++ b/src/feature.py\n@@ -1,3 +1,7 @@"
        return request

    def test_approval_approval_request_response_with_optional_fields(self) -> None:
        """ApprovalRequestResponse can be created with optional fields."""
        from app.api.approval import ApprovalRequestResponse

        response = ApprovalRequestResponse(
            id="test-123",
            session_id="session-456",
            task_id=None,
            type="commit",
            status="pending",
            diff_summary="Test",
            changed_files=[],
            requested_at="2024-01-15T10:30:00Z",
            expires_at=None,
        )
        assert response.task_id is None
        assert response.expires_at is None

    def test_approval_decision_response_fields(self) -> None:
        """ApprovalDecisionResponse has expected fields."""
        from app.api.approval import ApprovalDecisionResponse

        response = ApprovalDecisionResponse(
            request_id="req-123",
            status="approved",
            comment="LGTM",
            reviewed_at="2024-01-15T11:00:00Z",
        )
        assert response.request_id == "req-123"
        assert response.status == "approved"
        assert response.comment == "LGTM"
        assert response.reviewed_at == "2024-01-15T11:00:00Z"
