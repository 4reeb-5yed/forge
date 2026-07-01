"""Recovery API - Endpoints for crash recovery and session resume."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

logger = logging.getLogger(__name__)

recovery_router = APIRouter(prefix="/recovery", tags=["recovery"])


def _setup_recovery_routes(app: Any) -> None:
    """Add recovery routes to the app."""
    
    # GET /recovery/sessions - List sessions available for recovery
    @recovery_router.get("/sessions")
    async def list_recoverable_sessions() -> dict[str, Any]:
        """List all sessions that can be recovered from checkpoint."""
        try:
            from app.workflow.checkpoint_middleware import get_checkpoint_middleware
            middleware = get_checkpoint_middleware()
            if middleware is None:
                return {"sessions": [], "message": "Checkpoint middleware not available"}
            
            sessions = await middleware.list_recoverable_sessions()
            return {"sessions": sessions}
        except Exception as exc:
            logger.error("Failed to list recoverable sessions: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc)
            )

    # GET /recovery/sessions/{session_id} - Get checkpoint for a session
    @recovery_router.get("/sessions/{session_id}")
    async def get_session_checkpoint(session_id: str) -> dict[str, Any]:
        """Get the checkpoint data for a session."""
        try:
            from app.workflow.checkpoint_middleware import get_checkpoint_middleware
            middleware = get_checkpoint_middleware()
            if middleware is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Checkpoint middleware not available"
                )
            
            checkpoint = await middleware.recover(session_id)
            if checkpoint is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No checkpoint found for session {session_id}"
                )
            
            return {
                "session_id": session_id,
                "checkpoint": checkpoint,
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to get checkpoint for session %s: %s", session_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc)
            )

    # POST /recovery/sessions/{session_id}/resume - Resume from checkpoint
    @recovery_router.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        """Resume a session from its last checkpoint."""
        try:
            from app.workflow.checkpoint_middleware import get_checkpoint_middleware
            middleware = get_checkpoint_middleware()
            if middleware is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Checkpoint middleware not available"
                )
            
            checkpoint = await middleware.recover(session_id)
            if checkpoint is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No checkpoint found for session {session_id}"
                )
            
            # TODO: Wire into workflow execution to resume
            # For now, return the recovered state
            return {
                "session_id": session_id,
                "status": "ready_to_resume",
                "recovered_state": checkpoint,
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to resume session %s: %s", session_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc)
            )

    # Include router in app
    app.include_router(recovery_router)
