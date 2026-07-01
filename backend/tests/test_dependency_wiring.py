"""Regression tests: API layer uses RuntimeDeps assembled during bootstrap.

Verifies that:
- The API's AppDependencies receives the RuntimeInspector from RuntimeDeps
- The API's SessionManager is the same instance as in RuntimeDeps
- The API's InterruptHandler is the same instance as in RuntimeDeps
- GET /sessions/{id}/status does NOT return 503 (inspector_unavailable)
- Events published to the EventBus reach the WebSocket event store
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_deps():
    """Create the full app with lifespan to verify wiring."""
    from app.workflow.app import create_app

    app = create_app()
    return app


class TestDependencyWiring:
    """Verify API endpoints use the RuntimeDeps from bootstrap, not defaults."""

    def test_inspector_is_wired_after_startup(self, app_with_deps):
        """After lifespan startup, API's get_deps().inspector must not be None."""
        with TestClient(app_with_deps) as client:
            # The lifespan has run — check that API deps are wired
            from app.api import get_deps

            deps = get_deps()
            assert deps.inspector is not None, (
                "RuntimeInspector not wired into API layer. "
                "The lifespan must call set_deps() with inspector from RuntimeDeps."
            )

    def test_session_manager_is_shared(self, app_with_deps):
        """API and workflow must share the same SessionManager instance."""
        with TestClient(app_with_deps) as client:
            from app.api import get_deps

            api_deps = get_deps()
            runtime_deps = app_with_deps.state.deps

            assert api_deps.session_manager is runtime_deps.session_manager, (
                "API's SessionManager is not the same instance as RuntimeDeps. "
                "Sessions created via API won't be visible to the workflow layer."
            )

    def test_interrupt_handler_is_shared(self, app_with_deps):
        """API and workflow must share the same InterruptHandler instance."""
        with TestClient(app_with_deps) as client:
            from app.api import get_deps

            api_deps = get_deps()
            runtime_deps = app_with_deps.state.deps

            assert api_deps.interrupt_handler is runtime_deps.interrupt_handler, (
                "API's InterruptHandler is not the same instance as RuntimeDeps. "
                "Pause/resume commands won't affect the running workflow."
            )

    def test_status_endpoint_does_not_return_503(self, app_with_deps):
        """GET /sessions/{id}/status must not return 503 inspector_unavailable."""
        with TestClient(app_with_deps) as client:
            # Create a session first
            response = client.post(
                "/sessions",
                json={
                    "repo_url": "https://github.com/test/repo",
                    "goal": "test goal",
                },
            )
            assert response.status_code == 201
            session_id = response.json()["id"]

            # Query status — should NOT be 503
            status_response = client.get(f"/sessions/{session_id}/status")
            assert status_response.status_code != 503, (
                f"Got 503: {status_response.json()}. "
                "RuntimeInspector is not wired into the API layer."
            )

    def test_explain_endpoint_does_not_return_503(self, app_with_deps):
        """GET /sessions/{id}/explain must not return 503 inspector_unavailable."""
        with TestClient(app_with_deps) as client:
            # Create a session
            response = client.post(
                "/sessions",
                json={
                    "repo_url": "https://github.com/test/repo",
                    "goal": "test goal",
                },
            )
            session_id = response.json()["id"]

            # Query explain — should NOT be 503 (404 for no decision is acceptable)
            explain_response = client.get(f"/sessions/{session_id}/explain")
            assert explain_response.status_code != 503, (
                f"Got 503: {explain_response.json()}. "
                "RuntimeInspector is not wired into the API layer."
            )

    def test_capabilities_endpoint_does_not_return_503(self, app_with_deps):
        """GET /capabilities must not return 503."""
        with TestClient(app_with_deps) as client:
            response = client.get("/capabilities")
            assert response.status_code != 503, (
                f"Got 503: {response.json()}. "
                "RuntimeInspector is not wired into the API layer."
            )

    def test_event_bus_events_reach_event_store(self, app_with_deps):
        """Events published to EventBus should reach the API's event store."""
        with TestClient(app_with_deps) as client:
            from app.api import get_deps
            from app.runtime.events.models import Event, EventType

            runtime_deps = app_with_deps.state.deps
            api_deps = get_deps()

            # Publish an event via the event bus
            test_event = Event.create(
                type=EventType.TASK_START,
                session_id="test-session",
                source="test",
                payload={"task": "verify_wiring"},
                correlation_id="test-session",
                event_id="test-event-1",
            )

            # Run the async publish
            loop = asyncio.new_event_loop()
            loop.run_until_complete(runtime_deps.event_bus.publish(test_event))
            loop.close()

            # Check event store received it
            assert api_deps.event_store.has_session("test-session"), (
                "Event published to EventBus did not reach the API event store. "
                "The event bus subscriber is not wired correctly."
            )
