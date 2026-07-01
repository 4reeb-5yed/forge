"""Approval Gates API - REST endpoints for managing approval requests.

Provides endpoints for:
- Getting pending approval requests
- Approving/rejecting requests
- Getting diff details for review
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

logger = logging.getLogger(__name__)

approval_router = APIRouter(prefix="/approval", tags=["approval"])


def _init_approval_manager():
    """Get the approval manager from the app state."""
    try:
        from app.workflow.app import create_app
        # The approval manager should be wired into deps
        return None
    except Exception:
        return None


# Pydantic models for request/response
from pydantic import BaseModel, Field


class ApprovalRequestResponse(BaseModel):
    """Response for an approval request."""
    id: str
    session_id: str
    task_id: str | None
    type: str
    status: str
    diff_summary: str
    changed_files: list[str]
    requested_at: str
    expires_at: str | None = None


class ApprovalDecisionRequest(BaseModel):
    """Request body for approving/rejecting."""
    comment: str | None = Field(default=None, description="Optional review comment")


class ApprovalDecisionResponse(BaseModel):
    """Response for approval decision."""
    request_id: str
    status: str
    comment: str | None = None
    reviewed_at: str


class DiffResponse(BaseModel):
    """Response containing the full diff for review."""
    request_id: str
    diff: str
    changed_files: list[str]


def _setup_routes(app: Any) -> None:
    """Add approval routes to the app."""
    from fastapi import Depends

    def get_approval_manager() -> Any:
        """Get the approval manager from RuntimeDeps."""
        from app.api import get_deps
        deps = get_deps()
        if hasattr(deps, 'approval_manager') and deps.approval_manager:
            return deps.approval_manager
        # Fallback: try to get from global state
        from app.runtime.approval import _global_approval_manager
        return _global_approval_manager

    # GET /approval/pending/{session_id} - List pending approvals for a session
    @approval_router.get("/pending/{session_id}", response_model=list[ApprovalRequestResponse])
    async def list_pending_approvals(session_id: str) -> list[dict[str, Any]]:
        """List all pending approval requests for a session."""
        manager = get_approval_manager()
        if manager is None:
            return []

        requests = manager.get_pending_requests(session_id)
        return [
            {
                "id": r.id,
                "session_id": r.session_id,
                "task_id": r.task_id,
                "type": r.type.value,
                "status": r.status.value,
                "diff_summary": r.diff_summary,
                "changed_files": r.changed_files,
                "requested_at": r.requested_at.isoformat(),
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            }
            for r in requests
        ]

    # GET /approval/{request_id} - Get a specific approval request
    @approval_router.get("/{request_id}", response_model=ApprovalRequestResponse)
    async def get_approval_request(request_id: str) -> dict[str, Any]:
        """Get details of a specific approval request."""
        manager = get_approval_manager()
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Approval manager not available"
            )

        request = manager.get_request(request_id)
        if request is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request not found: {request_id}"
            )

        return {
            "id": request.id,
            "session_id": request.session_id,
            "task_id": request.task_id,
            "type": request.type.value,
            "status": request.status.value,
            "diff_summary": request.diff_summary,
            "changed_files": request.changed_files,
            "requested_at": request.requested_at.isoformat(),
            "expires_at": request.expires_at.isoformat() if request.expires_at else None,
        }

    # GET /approval/{request_id}/diff - Get full diff for review
    @approval_router.get("/{request_id}/diff", response_model=DiffResponse)
    async def get_approval_diff(request_id: str) -> dict[str, Any]:
        """Get the full diff for an approval request."""
        manager = get_approval_manager()
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Approval manager not available"
            )

        request = manager.get_request(request_id)
        if request is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request not found: {request_id}"
            )

        return {
            "request_id": request.id,
            "diff": request.diff_full or "",
            "changed_files": request.changed_files,
        }

    # POST /approval/{request_id}/approve - Approve an request
    @approval_router.post("/{request_id}/approve", response_model=ApprovalDecisionResponse)
    async def approve_request(
        request_id: str,
        body: ApprovalDecisionRequest | None = None
    ) -> dict[str, Any]:
        """Approve an approval request."""
        manager = get_approval_manager()
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Approval manager not available"
            )

        try:
            result = await manager.approve(
                request_id,
                reviewer="human",
                comment=body.comment if body else None
            )
            return {
                "request_id": result.request_id,
                "status": result.status.value,
                "comment": result.comment,
                "reviewed_at": result.reviewed_at.isoformat() if result.reviewed_at else None,
            }
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )

    # POST /approval/{request_id}/reject - Reject an request
    @approval_router.post("/{request_id}/reject", response_model=ApprovalDecisionResponse)
    async def reject_request(
        request_id: str,
        body: ApprovalDecisionRequest | None = None
    ) -> dict[str, Any]:
        """Reject an approval request."""
        manager = get_approval_manager()
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Approval manager not available"
            )

        try:
            result = await manager.reject(
                request_id,
                reviewer="human",
                comment=body.comment if body else None
            )
            return {
                "request_id": result.request_id,
                "status": result.status.value,
                "comment": result.comment,
                "reviewed_at": result.reviewed_at.isoformat() if result.reviewed_at else None,
            }
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )


# Auto-register routes at module load time (decorators register them)
_setup_routes(None)
