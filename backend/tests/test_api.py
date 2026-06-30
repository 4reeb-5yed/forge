"""Unit tests for the Application Layer REST and WebSocket endpoints.

Tests session management, messaging, artifact retrieval, control endpoints,
runtime inspection, and WebSocket event streaming.

Requirements: 26.1, 26.2, 26.3, 26.4
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import (
    AppDependencies,
    SessionEventStore,
    create_app,
    set_deps,
)
from app.runtime.events.models import (
    DecisionKind,
    DecisionRecord,
    Event,
    EventType,
)
from app.runtime.inspector import (
    DecisionNotFoundError,
    RuntimeInspector,
    RuntimeStatus,
    TaskView,
    WorkerStatus,
)
from app.runtime.interrupt import InterruptHandler, NotPausedError
from app.runtime.session import SessionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_manager() -> SessionManager:
    """Fresh SessionManager instance."""
    return SessionManager()


@pytest.fixture
def interrupt_handler() -> InterruptHandler:
    """Fresh InterruptHandler instance."""
    return InterruptHandler()


@pytest.fixture
def event_store() -> SessionEventStore:
    """Fresh event store for WebSocket tests."""
    return SessionEventStore()


@pytest.fixture
def mock_inspector() -> MagicMock:
    """Mock RuntimeInspector that returns canned responses."""
    inspector = MagicMock(spec=RuntimeInspector)
    inspector.get_status.return_value = RuntimeStatus(
        current_node="idle",
        worker_status=WorkerStatus(worker_id="worker-0", status="idle"),
        task_queue=[],
        active_task=None,
        budget=None,
    )
    inspector.explain_last_decision.return_value = DecisionRecord(
        kind=DecisionKind.RETRY,
        subject="task-1",
        inputs={"attempt": 1},
        decision="retry",
        rationale="Tests failed, retry budget remaining",
        alternatives=["escalate", "skip"],
    )
    inspector.capability_summary.return_value = {
        "available": {"ai_coder": "healthy"},
        "degraded": [],
        "missing_required": [],
        "missing_reasons": {},
        "soft_degradations": [],
        "mode": "operational",
        "can_operate": True,
    }
    return inspector


@pytest.fixture
def deps(
    session_manager: SessionManager,
    interrupt_handler: InterruptHandler,
    event_store: SessionEventStore,
    mock_inspector: MagicMock,
) -> AppDependencies:
    """Application dependencies with test doubles."""
    return AppDependencies(
        session_manager=session_manager,
        inspector=mock_inspector,
        interrupt_handler=interrupt_handler,
        event_store=event_store,
    )


@pytest.fixture
def app(deps: AppDependencies):
    """FastAPI app instance for testing."""
    return create_app(deps)


@pytest.fixture
async def client(app) -> AsyncClient:
    """httpx async client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Session Management Endpoints
# ---------------------------------------------------------------------------


class TestSessionCRUD:
    """Tests for session CRUD endpoints."""

    async def test_create_session_success(self, client: AsyncClient) -> None:
        """POST /sessions creates a session and returns 201."""
        response = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build a REST API",
                "build_mode": "new",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        assert data["repo_url"] == "https://github.com/user/repo"
        assert data["goal"] == "Build a REST API"
        assert data["build_mode"] == "new"
        assert data["status"] == "created"
        assert "created_at" in data

    async def test_create_session_invalid_url(self, client: AsyncClient) -> None:
        """POST /sessions with invalid URL returns 422."""
        response = await client.post(
            "/sessions",
            json={
                "repo_url": "not-a-url",
                "goal": "Build something",
            },
        )
        assert response.status_code == 422
        data = response.json()
        assert data["detail"]["field"] == "repo_url"

    async def test_create_session_empty_goal(self, client: AsyncClient) -> None:
        """POST /sessions with empty goal returns 422."""
        response = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "",
            },
        )
        assert response.status_code == 422
        data = response.json()
        assert data["detail"]["field"] == "goal"

    async def test_list_sessions_empty(self, client: AsyncClient) -> None:
        """GET /sessions returns empty list when no sessions exist."""
        response = await client.get("/sessions")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_sessions_after_create(self, client: AsyncClient) -> None:
        """GET /sessions returns all created sessions."""
        await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo1",
                "goal": "Goal 1",
            },
        )
        await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo2",
                "goal": "Goal 2",
            },
        )
        response = await client.get("/sessions")
        assert response.status_code == 200
        assert len(response.json()) == 2

    async def test_get_session_success(self, client: AsyncClient) -> None:
        """GET /sessions/{id} returns the session detail."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.get(f"/sessions/{session_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == session_id
        assert data["status"] == "created"

    async def test_get_session_not_found(self, client: AsyncClient) -> None:
        """GET /sessions/{id} returns 404 for non-existent session."""
        response = await client.get("/sessions/nonexistent-id")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "not_found"

    async def test_delete_session_success(self, client: AsyncClient) -> None:
        """DELETE /sessions/{id} removes the session."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.delete(f"/sessions/{session_id}")
        assert response.status_code == 204

        # Verify session is gone
        get_resp = await client.get(f"/sessions/{session_id}")
        assert get_resp.status_code == 404

    async def test_delete_session_not_found(self, client: AsyncClient) -> None:
        """DELETE /sessions/{id} returns 404 for non-existent session."""
        response = await client.delete("/sessions/nonexistent-id")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Messaging Endpoint
# ---------------------------------------------------------------------------


class TestMessaging:
    """Tests for the messaging endpoint."""

    async def test_send_message_success(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/messages accepts a message."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.post(
            f"/sessions/{session_id}/messages",
            json={"content": "Please add tests"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == session_id
        assert data["status"] == "accepted"
        assert data["content"] == "Please add tests"

    async def test_send_message_session_not_found(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/messages returns 404 for non-existent session."""
        response = await client.post(
            "/sessions/nonexistent-id/messages",
            json={"content": "Hello"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Artifact Retrieval Endpoints
# ---------------------------------------------------------------------------


class TestArtifactRetrieval:
    """Tests for artifact retrieval endpoints."""

    async def test_get_spec_session_not_found(self, client: AsyncClient) -> None:
        """GET /sessions/{id}/artifacts/spec returns 404 for non-existent session."""
        response = await client.get("/sessions/nonexistent/artifacts/spec")
        assert response.status_code == 404

    async def test_get_spec_no_artifact(self, client: AsyncClient) -> None:
        """GET /sessions/{id}/artifacts/spec returns 404 when no spec exists."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.get(f"/sessions/{session_id}/artifacts/spec")
        assert response.status_code == 404
        assert "No specification artifact" in response.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Control Endpoints (interrupt, resume, redirect, stop)
# ---------------------------------------------------------------------------


class TestControlEndpoints:
    """Tests for interrupt, resume, redirect, and stop."""

    async def test_interrupt_success(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/interrupt pauses the build."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.post(f"/sessions/{session_id}/interrupt")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "paused"
        assert data["session_id"] == session_id

    async def test_interrupt_session_not_found(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/interrupt returns 404 for non-existent session."""
        response = await client.post("/sessions/nonexistent/interrupt")
        assert response.status_code == 404

    async def test_resume_success(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/resume resumes a paused build."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        # First pause
        await client.post(f"/sessions/{session_id}/interrupt")

        # Then resume
        response = await client.post(f"/sessions/{session_id}/resume")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "resumed"

    async def test_resume_not_paused(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/resume returns 409 when not paused."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.post(f"/sessions/{session_id}/resume")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "not_paused"

    async def test_redirect_success(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/redirect redirects a paused build."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        # First pause
        await client.post(f"/sessions/{session_id}/interrupt")

        # Then redirect
        response = await client.post(
            f"/sessions/{session_id}/redirect",
            json={"direction": "Focus on testing instead"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "redirected"
        assert data["direction"] == "Focus on testing instead"

    async def test_redirect_empty_direction(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/redirect returns 422 for empty direction."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        # First pause
        await client.post(f"/sessions/{session_id}/interrupt")

        response = await client.post(
            f"/sessions/{session_id}/redirect",
            json={"direction": ""},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "empty_direction"

    async def test_redirect_not_paused(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/redirect returns 409 when not paused."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.post(
            f"/sessions/{session_id}/redirect",
            json={"direction": "New direction"},
        )
        assert response.status_code == 409

    async def test_stop_success(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/stop stops the build."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.post(f"/sessions/{session_id}/stop")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "stopped"

    async def test_stop_session_not_found(self, client: AsyncClient) -> None:
        """POST /sessions/{id}/stop returns 404 for non-existent session."""
        response = await client.post("/sessions/nonexistent/stop")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Runtime Inspection Endpoints
# ---------------------------------------------------------------------------


class TestRuntimeInspection:
    """Tests for status, explain, and capabilities endpoints.

    Requirement 26.4: responses served solely from RuntimeInspector, no AI.
    """

    async def test_get_status_success(self, client: AsyncClient) -> None:
        """GET /sessions/{id}/status returns runtime status from inspector."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.get(f"/sessions/{session_id}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["current_node"] == "idle"
        assert data["worker_status"]["status"] == "idle"
        assert data["task_queue"] == []
        assert data["active_task"] is None

    async def test_get_status_session_not_found(self, client: AsyncClient) -> None:
        """GET /sessions/{id}/status returns 404 for non-existent session."""
        response = await client.get("/sessions/nonexistent/status")
        assert response.status_code == 404

    async def test_get_explain_success(self, client: AsyncClient) -> None:
        """GET /sessions/{id}/explain returns last decision from inspector."""
        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.get(f"/sessions/{session_id}/explain")
        assert response.status_code == 200
        data = response.json()
        assert data["kind"] == "retry"
        assert data["subject"] == "task-1"
        assert data["decision"] == "retry"
        assert "alternatives" in data

    async def test_get_explain_no_decision(
        self, client: AsyncClient, mock_inspector: MagicMock
    ) -> None:
        """GET /sessions/{id}/explain returns 404 when no decision exists."""
        mock_inspector.explain_last_decision.side_effect = DecisionNotFoundError("test-session")

        create_resp = await client.post(
            "/sessions",
            json={
                "repo_url": "https://github.com/user/repo",
                "goal": "Build it",
            },
        )
        session_id = create_resp.json()["id"]

        response = await client.get(f"/sessions/{session_id}/explain")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "no_decision"

    async def test_get_capabilities(self, client: AsyncClient) -> None:
        """GET /capabilities returns capability summary from inspector."""
        response = await client.get("/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert data["can_operate"] is True
        assert data["mode"] == "operational"
        assert "available" in data


# ---------------------------------------------------------------------------
# WebSocket Event Stream
# ---------------------------------------------------------------------------


class TestWebSocketEventStream:
    """Tests for the WebSocket event stream endpoint."""

    async def test_ws_reject_nonexistent_session(self, app, deps: AppDependencies) -> None:
        """WS /sessions/{id}/events rejects connection for non-existent session.

        Requirement 26.3
        """
        from starlette.testclient import TestClient

        with TestClient(app) as test_client:
            with pytest.raises(Exception):
                # WebSocket connection for a non-existent session should be rejected
                with test_client.websocket_connect("/sessions/nonexistent/events") as ws:
                    pass  # Should not reach here

    async def test_ws_accepts_existing_session(
        self, app, deps: AppDependencies
    ) -> None:
        """WS /sessions/{id}/events accepts connection for existing session.

        Requirement 26.2
        """
        from starlette.testclient import TestClient

        # Create a session first
        session = deps.session_manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build it",
        )

        with TestClient(app) as test_client:
            with test_client.websocket_connect(f"/sessions/{session.id}/events") as ws:
                # Connection accepted; push an event and verify it's received
                event = Event.create(
                    type=EventType.TASK_START,
                    session_id=session.id,
                    source="test",
                    payload={"task_id": "task-1"},
                    seq=1,
                )

                # Push event to the store in a background thread
                import threading

                def push_event():
                    import asyncio
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(deps.event_store.append(session.id, event))
                    loop.close()

                t = threading.Thread(target=push_event)
                t.start()
                t.join(timeout=5)

                # Receive the event
                data = ws.receive_json()
                assert data["type"] == "task.start"
                assert data["seq"] == 1
                assert data["payload"]["task_id"] == "task-1"

    async def test_ws_events_in_seq_order(
        self, app, deps: AppDependencies
    ) -> None:
        """WS /sessions/{id}/events delivers events in strictly increasing seq order.

        Requirement 26.2
        """
        from starlette.testclient import TestClient

        session = deps.session_manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build it",
        )

        with TestClient(app) as test_client:
            with test_client.websocket_connect(f"/sessions/{session.id}/events") as ws:
                # Push multiple events out of order
                events = [
                    Event.create(
                        type=EventType.TASK_START,
                        session_id=session.id,
                        source="test",
                        payload={"task_id": "task-1"},
                        seq=2,
                    ),
                    Event.create(
                        type=EventType.TASK_DONE,
                        session_id=session.id,
                        source="test",
                        payload={"task_id": "task-1"},
                        seq=1,
                    ),
                    Event.create(
                        type=EventType.VERIFY_PASSED,
                        session_id=session.id,
                        source="test",
                        payload={"task_id": "task-1"},
                        seq=3,
                    ),
                ]

                import threading

                def push_events():
                    import asyncio
                    loop = asyncio.new_event_loop()
                    for ev in events:
                        loop.run_until_complete(deps.event_store.append(session.id, ev))
                    loop.close()

                t = threading.Thread(target=push_events)
                t.start()
                t.join(timeout=5)

                # Receive events and verify ordering
                received = []
                for _ in range(3):
                    data = ws.receive_json()
                    received.append(data)

                seqs = [d["seq"] for d in received]
                assert seqs == sorted(seqs), f"Events not in seq order: {seqs}"


# ---------------------------------------------------------------------------
# SessionEventStore unit tests
# ---------------------------------------------------------------------------


class TestSessionEventStore:
    """Tests for the in-memory event store."""

    async def test_append_and_get(self) -> None:
        """Events can be appended and retrieved."""
        store = SessionEventStore()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="test",
            payload={},
            seq=1,
        )
        await store.append("s1", event)
        events = await store.get_events_from("s1", from_seq=1)
        assert len(events) == 1
        assert events[0].seq == 1

    async def test_wait_for_events_returns_immediately_if_available(self) -> None:
        """wait_for_events returns immediately when events already exist."""
        store = SessionEventStore()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="test",
            payload={},
            seq=5,
        )
        await store.append("s1", event)
        events = await store.wait_for_events("s1", after_seq=0, timeout=1.0)
        assert len(events) == 1

    async def test_wait_for_events_timeout(self) -> None:
        """wait_for_events returns empty list on timeout."""
        store = SessionEventStore()
        events = await store.wait_for_events("s1", after_seq=0, timeout=0.1)
        assert events == []

    async def test_remove_session(self) -> None:
        """remove_session clears the session's events."""
        store = SessionEventStore()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="test",
            payload={},
            seq=1,
        )
        await store.append("s1", event)
        store.remove_session("s1")
        assert not store.has_session("s1")
