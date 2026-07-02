"""Tests for WebSocket authentication.

Verifies that the WebSocket event stream endpoint requires authentication
and rejects unauthenticated connections.

Requirements: 26.5, 26.6
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from app.api.auth import (
    WebSocketAuthError,
    require_ws_auth,
    set_api_token,
    set_auth_disabled,
    reset_auth_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-websocket-token-abc123"


@pytest.fixture(autouse=True)
def _setup_auth():
    """Set a known test token and ensure auth is enabled for each test."""
    set_api_token(TEST_TOKEN)
    set_auth_disabled(False)
    yield
    reset_auth_state()


@pytest.fixture
def app() -> FastAPI:
    """Create a minimal FastAPI app with an auth-protected WebSocket endpoint."""
    app = FastAPI()

    @app.websocket("/ws/stream")
    async def ws_endpoint(websocket: WebSocket):
        try:
            token = await require_ws_auth(websocket)
        except WebSocketAuthError:
            await websocket.close(code=1008, reason="Authentication failed")
            return
        await websocket.accept()
        await websocket.send_json({"connected": True, "token": token})
        await websocket.close()

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Test client for the minimal app."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# WebSocket Authentication Tests
# ---------------------------------------------------------------------------


class TestWebSocketAuthRequired:
    """Test that WebSocket connections require authentication."""

    def test_connection_without_token_is_rejected(self, client: TestClient) -> None:
        """Connection with no token should be rejected with close code 1008."""
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream") as ws:
                ws.receive_json()

    def test_connection_with_wrong_token_is_rejected(self, client: TestClient) -> None:
        """Connection with wrong token should be rejected."""
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream?token=wrong-token") as ws:
                ws.receive_json()

    def test_connection_with_correct_token_succeeds(self, client: TestClient) -> None:
        """Connection with correct token should succeed."""
        with client.websocket_connect(f"/ws/stream?token={TEST_TOKEN}") as ws:
            data = ws.receive_json()
            assert data["connected"] is True
            assert data["token"] == TEST_TOKEN


class TestWebSocketAuthDisabled:
    """Test that WebSocket connections work when auth is disabled."""

    def test_connection_succeeds_without_token_when_auth_disabled(self, client: TestClient) -> None:
        """Connection without token should succeed when auth is disabled."""
        set_auth_disabled(True)
        try:
            with client.websocket_connect("/ws/stream") as ws:
                data = ws.receive_json()
                assert data["connected"] is True
                assert data["token"] == "auth-disabled"
        finally:
            set_auth_disabled(False)

    def test_connection_with_token_succeeds_when_auth_disabled(self, client: TestClient) -> None:
        """Connection with token should succeed when auth is disabled."""
        set_auth_disabled(True)
        try:
            with client.websocket_connect(f"/ws/stream?token={TEST_TOKEN}") as ws:
                data = ws.receive_json()
                assert data["connected"] is True
        finally:
            set_auth_disabled(False)


class TestWebSocketAuthEdgeCases:
    """Edge cases for WebSocket authentication."""

    def test_empty_token_is_rejected(self, client: TestClient) -> None:
        """Connection with empty token should be rejected."""
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream?token=") as ws:
                ws.receive_json()

    def test_token_with_spaces_is_rejected(self, client: TestClient) -> None:
        """Connection with token containing spaces should be rejected."""
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream?token=token%20with%20spaces") as ws:
                ws.receive_json()
