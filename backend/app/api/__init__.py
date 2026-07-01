"""Application Layer - FastAPI REST and WebSocket endpoints.

Exposes session-management, messaging, artifact-retrieval, control,
and runtime-inspection endpoints over REST and one WebSocket event
stream per session.

Contains no engineering logic — translates between HTTP/WS transport and
the runtime layer.

Requirements: 26.1, 26.2, 26.3, 26.4
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from app.runtime.events.models import Event
from app.runtime.inspector import (
    DecisionNotFoundError,
    RuntimeInspector,
    TaskNotFoundError,
)
from app.runtime.interrupt import (
    EmptyDirectionError,
    InterruptHandler,
    NotPausedError,
)
from app.runtime.session import (
    SessionManager,
    SessionNotFoundError,
    SessionValidationError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """Request body for creating a new session."""

    repo_url: str = Field(..., description="Repository URL (http/https)")
    goal: str = Field(..., description="Plain-English build goal")
    build_mode: str = Field(default="new", description="Build mode: new, extend, analyze, document")
    vcs_token: str = Field(default="", description="VCS access token")


class SessionResponse(BaseModel):
    """Response body for a session."""

    id: str
    repo_url: str
    goal: str
    build_mode: str
    status: str
    created_at: str


class MessageRequest(BaseModel):
    """Request body for sending a developer message."""

    content: str = Field(..., description="Message content")


class RedirectRequest(BaseModel):
    """Request body for redirecting a paused build."""

    direction: str = Field(..., description="New direction for the build")


# ---------------------------------------------------------------------------
# Event stream store (in-memory, per-session event buffer)
# ---------------------------------------------------------------------------


class SessionEventStore:
    """In-memory per-session event buffer for WebSocket streaming.

    Stores events per session and allows subscribers to receive events
    in seq order. This is a simple in-memory implementation that backs
    the WebSocket event stream.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._conditions: dict[str, asyncio.Condition] = {}

    def _ensure_session(self, session_id: str) -> None:
        if session_id not in self._events:
            self._events[session_id] = []
            self._conditions[session_id] = asyncio.Condition()

    async def append(self, session_id: str, event: Event) -> None:
        """Append an event to the session buffer and notify waiters."""
        self._ensure_session(session_id)
        self._events[session_id].append(event)
        async with self._conditions[session_id]:
            self._conditions[session_id].notify_all()

    async def get_events_from(self, session_id: str, from_seq: int) -> list[Event]:
        """Get events with seq >= from_seq for a session."""
        self._ensure_session(session_id)
        return [e for e in self._events[session_id] if e.seq >= from_seq]

    async def wait_for_events(self, session_id: str, after_seq: int, timeout: float = 30.0) -> list[Event]:
        """Wait for new events after the given seq, with a timeout."""
        self._ensure_session(session_id)
        condition = self._conditions[session_id]

        async with condition:
            # Check if events are already available
            events = [e for e in self._events[session_id] if e.seq > after_seq]
            if events:
                return sorted(events, key=lambda e: e.seq)

            # Wait for new events
            try:
                await asyncio.wait_for(condition.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        return sorted(
            [e for e in self._events[session_id] if e.seq > after_seq],
            key=lambda e: e.seq,
        )

    def has_session(self, session_id: str) -> bool:
        """Check if a session exists in the event store."""
        return session_id in self._events

    def remove_session(self, session_id: str) -> None:
        """Remove a session's events from the store."""
        self._events.pop(session_id, None)
        self._conditions.pop(session_id, None)


# ---------------------------------------------------------------------------
# Application dependencies (wired at startup)
# ---------------------------------------------------------------------------


class AppDependencies:
    """Container for runtime dependencies injected into the API layer.

    The Application Layer holds references to runtime components but does
    not create them — they are wired during application bootstrap.
    """

    def __init__(
        self,
        *,
        session_manager: SessionManager | None = None,
        inspector: RuntimeInspector | None = None,
        interrupt_handler: InterruptHandler | None = None,
        event_store: SessionEventStore | None = None,
    ) -> None:
        self.session_manager = session_manager or SessionManager()
        self.inspector = inspector
        self.interrupt_handler = interrupt_handler or InterruptHandler()
        self.event_store = event_store or SessionEventStore()


# Module-level dependencies (set during app startup)
_deps: AppDependencies | None = None

# Reference to the main FastAPI app (set during create_app in workflow/app.py)
_app_ref = None


def get_deps() -> AppDependencies:
    """Get the application dependencies.
    
    Reads from module-level _deps which is set by set_deps() during lifespan.
    If not yet set, creates a default (only during testing).
    """
    global _deps
    if _deps is None:
        _deps = AppDependencies()
    return _deps


def set_deps(deps: AppDependencies) -> None:
    """Set the application dependencies (called at startup)."""
    global _deps
    _deps = deps


def set_app_ref(app) -> None:
    """Store a reference to the main FastAPI app (unused, kept for compat)."""
    global _app_ref
    _app_ref = app


# ---------------------------------------------------------------------------
# Helper to serialize session to response
# ---------------------------------------------------------------------------


def _session_to_response(session: Any) -> dict[str, Any]:
    """Convert a Session dataclass to a serializable dict."""
    return {
        "id": session.id,
        "repo_url": session.repo_url,
        "goal": session.goal,
        "build_mode": session.build_mode.value if hasattr(session.build_mode, "value") else str(session.build_mode),
        "status": session.status,
        "created_at": session.created_at.isoformat(),
    }


def _serialize_event(event: Event) -> dict[str, Any]:
    """Serialize an Event to a JSON-compatible dict."""
    return {
        "schema_version": event.schema_version,
        "seq": event.seq,
        "session_id": event.session_id,
        "type": event.type.value if hasattr(event.type, "value") else str(event.type),
        "timestamp": event.timestamp.isoformat(),
        "source": event.source,
        "payload": event.payload,
        "causation_id": event.causation_id,
        "correlation_id": event.correlation_id,
        "event_id": event.event_id,
    }


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_app(deps: AppDependencies | None = None) -> FastAPI:
    """Create and configure the FastAPI application with all endpoints.

    Args:
        deps: Optional application dependencies. If None, default instances
              are created.

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title="Forge Runtime API", version="0.1.0")

    if deps is not None:
        set_deps(deps)

    # ------------------------------------------------------------------
    # Session Management Endpoints (CRUD)
    # ------------------------------------------------------------------

    @app.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        """Create a new build session.

        Requirements: 1.1, 1.2, 1.7
        """
        deps = get_deps()
        try:
            session = deps.session_manager.create_session(
                repo_url=request.repo_url,
                goal=request.goal,
                build_mode=request.build_mode,
                vcs_token=request.vcs_token,
            )
        except SessionValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"field": e.field, "reason": e.reason},
            )
        return _session_to_response(session)

    @app.get("/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        """List all sessions.

        Requirements: 1.3
        """
        deps = get_deps()
        sessions = deps.session_manager.list_sessions()
        return [_session_to_response(s) for s in sessions]

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        """Get a session by its identifier.

        Requirements: 1.4, 1.6
        """
        deps = get_deps()
        try:
            session = deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )
        return _session_to_response(session)

    @app.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_session(session_id: str) -> None:
        """Delete a session and clean up resources.

        Requirements: 1.5, 1.6, 1.8
        """
        deps = get_deps()
        try:
            deps.session_manager.delete_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )
        # Also remove from event store
        deps.event_store.remove_session(session_id)

    # ------------------------------------------------------------------
    # Messaging Endpoint
    # ------------------------------------------------------------------

    @app.post("/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: MessageRequest) -> dict[str, Any]:
        """Send a developer message to the session.

        This endpoint accepts the message and acknowledges receipt.
        The workflow engine processes it asynchronously.

        Requirements: 2.1
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        # Acknowledge message receipt
        return {
            "session_id": session_id,
            "status": "accepted",
            "content": request.content,
        }

    # ------------------------------------------------------------------
    # Artifact Retrieval Endpoints
    # ------------------------------------------------------------------

    @app.get("/sessions/{session_id}/artifacts/spec")
    async def get_specification(session_id: str) -> dict[str, Any]:
        """Get the current specification artifact for a session.

        Requirements: 4.4, 4.7
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        # Specification artifact retrieval is a placeholder — in the full
        # runtime this would query the artifact store. For now, return
        # not-found when no spec exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "not_found",
                "session_id": session_id,
                "message": "No specification artifact exists for this session",
            },
        )

    # ------------------------------------------------------------------
    # Control Endpoints (interrupt, resume, redirect, stop)
    # ------------------------------------------------------------------

    @app.post("/sessions/{session_id}/interrupt")
    async def interrupt_session(session_id: str) -> dict[str, Any]:
        """Pause/interrupt the session's build.

        Requirements: 24.1, 24.2, 24.7
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        paused_state = await deps.interrupt_handler.pause(
            session_id=session_id,
            message="Build paused by developer request",
        )
        return {
            "session_id": session_id,
            "status": "paused",
            "message": paused_state.interrupt_message,
        }

    @app.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        """Resume a paused session.

        Requirements: 24.3, 24.4
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        try:
            retained_state = await deps.interrupt_handler.resume(session_id)
        except NotPausedError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "not_paused",
                    "session_id": session_id,
                    "message": "No paused build exists for this session",
                },
            )
        return {
            "session_id": session_id,
            "status": "resumed",
            "retained_state": retained_state,
        }

    @app.post("/sessions/{session_id}/redirect")
    async def redirect_session(session_id: str, request: RedirectRequest) -> dict[str, Any]:
        """Redirect a paused build with a new direction.

        Requirements: 24.5, 24.6
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        try:
            result = await deps.interrupt_handler.redirect(session_id, request.direction)
        except EmptyDirectionError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "empty_direction",
                    "session_id": session_id,
                    "message": "A non-empty direction is required",
                },
            )
        except NotPausedError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "not_paused",
                    "session_id": session_id,
                    "message": "No paused build exists for this session",
                },
            )
        return {
            "session_id": session_id,
            "status": "redirected",
            "direction": result["direction"],
        }

    @app.post("/sessions/{session_id}/stop")
    async def stop_session(session_id: str) -> dict[str, Any]:
        """Stop the session's build entirely.

        Requirements: 24.7
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        await deps.interrupt_handler.stop(session_id)
        return {
            "session_id": session_id,
            "status": "stopped",
        }

    # ------------------------------------------------------------------
    # Runtime Inspection Endpoints (status, explain, capabilities)
    # Requirement 26.4: served solely from RuntimeInspector, no AI
    # ------------------------------------------------------------------

    @app.get("/sessions/{session_id}/status")
    async def get_runtime_status(session_id: str) -> dict[str, Any]:
        """Get the current runtime status for a session.

        Requirements: 20.1, 20.2, 26.4
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        if deps.inspector is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "inspector_unavailable", "message": "Runtime inspector not configured"},
            )

        runtime_status = deps.inspector.get_status(session_id)
        return {
            "session_id": session_id,
            "current_node": runtime_status.current_node,
            "worker_status": {
                "worker_id": runtime_status.worker_status.worker_id,
                "status": runtime_status.worker_status.status,
                "current_task_id": runtime_status.worker_status.current_task_id,
            },
            "task_queue": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                }
                for t in runtime_status.task_queue
            ],
            "active_task": {
                "id": runtime_status.active_task.id,
                "title": runtime_status.active_task.title,
                "status": runtime_status.active_task.status,
            } if runtime_status.active_task else None,
            "budget": runtime_status.budget,
        }

    @app.get("/sessions/{session_id}/explain")
    async def get_explanation(session_id: str) -> dict[str, Any]:
        """Get the last decision explanation for a session.

        Requirements: 20.3, 20.4, 26.4
        """
        deps = get_deps()
        # Verify session exists
        try:
            deps.session_manager.get_session(session_id)
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "session_id": session_id},
            )

        if deps.inspector is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "inspector_unavailable", "message": "Runtime inspector not configured"},
            )

        try:
            decision = deps.inspector.explain_last_decision(session_id)
        except DecisionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "no_decision",
                    "session_id": session_id,
                    "message": "No decision record found for this session",
                },
            )
        return {
            "session_id": session_id,
            "kind": decision.kind.value if hasattr(decision.kind, "value") else str(decision.kind),
            "subject": decision.subject,
            "inputs": decision.inputs,
            "decision": decision.decision,
            "rationale": decision.rationale,
            "alternatives": decision.alternatives,
        }

    @app.get("/capabilities")
    async def get_capabilities() -> dict[str, Any]:
        """Get the capability summary from the runtime.

        Requirements: 20.7, 26.4
        """
        deps = get_deps()
        if deps.inspector is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "inspector_unavailable", "message": "Runtime inspector not configured"},
            )

        return deps.inspector.capability_summary()

    # ------------------------------------------------------------------
    # WebSocket Event Stream
    # Requirement 26.2: forward events in strictly increasing seq order
    # Requirement 26.3: reject connection for non-existent session
    # ------------------------------------------------------------------

    @app.websocket("/sessions/{session_id}/events")
    async def session_event_stream(websocket: WebSocket, session_id: str) -> None:
        """WebSocket event stream for a session.

        Forwards each typed Event for the session in strictly increasing
        seq order without adding engineering logic.

        Requirements: 26.2, 26.3
        """
        deps = get_deps()

        # Requirement 26.3: reject connection for non-existent session
        if not deps.session_manager.has_session(session_id):
            await websocket.close(code=4004, reason=f"Session not found: {session_id}")
            return

        await websocket.accept()

        last_seq = 0
        try:
            while True:
                # Wait for new events after the last seen seq
                events = await deps.event_store.wait_for_events(
                    session_id, after_seq=last_seq, timeout=30.0
                )

                # Forward events in strictly increasing seq order
                for event in sorted(events, key=lambda e: e.seq):
                    if event.seq > last_seq:
                        await websocket.send_json(_serialize_event(event))
                        last_seq = event.seq

        except WebSocketDisconnect:
            logger.info("WebSocket disconnected for session %s", session_id)
        except Exception as e:
            logger.error("WebSocket error for session %s: %s", session_id, e)
            try:
                await websocket.close(code=1011, reason="Internal error")
            except Exception:
                pass

    return app
