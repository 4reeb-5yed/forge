"""Unit tests for Authentication and Authorization.

Tests that all endpoints require valid credentials and reject requests
with missing or invalid credentials with a proper error response.

Requirements: 26.5, 26.6
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request, WebSocket, status
from fastapi.testclient import TestClient

from app.api.auth import (
    AUTH_ERROR_BODY,
    WebSocketAuthError,
    require_auth,
    require_ws_auth,
    reset_auth_state,
    set_api_token,
    set_auth_disabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-forge-token-abc123"


@pytest.fixture(autouse=True)
def _setup_auth():
    """Set a known test token and ensure auth is enabled for each test."""
    set_api_token(TEST_TOKEN)
    set_auth_disabled(False)
    yield
    reset_auth_state()


@pytest.fixture
def app() -> FastAPI:
    """Create a minimal FastAPI app with an auth-protected endpoint."""
    app = FastAPI()

    @app.get("/protected")
    async def protected_endpoint(token: str = Depends(require_auth)):
        return {"message": "success", "token_used": token}

    @app.post("/action")
    async def action_endpoint(token: str = Depends(require_auth)):
        return {"action": "performed"}

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
# Requirement 26.5: Require valid auth on all endpoints
# ---------------------------------------------------------------------------


class TestValidAuth:
    """Test that valid credentials grant access."""

    def test_valid_bearer_token_grants_access(self, client: TestClient) -> None:
        response = client.get(
            "/protected",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["message"] == "success"

    def test_valid_token_returned_to_endpoint(self, client: TestClient) -> None:
        response = client.get(
            "/protected",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert response.json()["token_used"] == TEST_TOKEN

    def test_valid_token_on_post_endpoint(self, client: TestClient) -> None:
        response = client.post(
            "/action",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["action"] == "performed"

    def test_valid_token_on_websocket_header(self, app: FastAPI) -> None:
        client = TestClient(app)
        with client.websocket_connect(
            "/ws/stream",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ) as ws:
            data = ws.receive_json()
            assert data["connected"] is True
            assert data["token"] == TEST_TOKEN

    def test_valid_token_on_websocket_query_param(self, app: FastAPI) -> None:
        client = TestClient(app)
        with client.websocket_connect(f"/ws/stream?token={TEST_TOKEN}") as ws:
            data = ws.receive_json()
            assert data["connected"] is True


# ---------------------------------------------------------------------------
# Requirement 26.6: Reject missing/invalid credentials
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    """Test rejection when no credentials are provided."""

    def test_no_auth_header_returns_401(self, client: TestClient) -> None:
        response = client.get("/protected")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_no_auth_header_returns_error_body(self, client: TestClient) -> None:
        response = client.get("/protected")
        body = response.json()
        assert body["detail"]["error"] == "authentication_failure"
        assert "credentials" in body["detail"]["detail"].lower()

    def test_no_auth_header_includes_www_authenticate(self, client: TestClient) -> None:
        response = client.get("/protected")
        assert response.headers.get("www-authenticate") == "Bearer"

    def test_no_auth_on_post_returns_401(self, client: TestClient) -> None:
        response = client.post("/action")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_websocket_no_token_closes_connection(self, app: FastAPI) -> None:
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream") as ws:
                ws.receive_json()


class TestInvalidCredentials:
    """Test rejection when credentials are present but invalid."""

    def test_wrong_token_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer wrong-token-value"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_wrong_token_returns_error_body(self, client: TestClient) -> None:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer wrong-token-value"},
        )
        body = response.json()
        assert body["detail"]["error"] == "authentication_failure"

    def test_empty_bearer_token_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer "},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_non_bearer_scheme_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/protected",
            headers={"Authorization": f"Basic {TEST_TOKEN}"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_malformed_auth_header_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/protected",
            headers={"Authorization": "not-a-valid-header"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_websocket_wrong_token_closes_connection(self, app: FastAPI) -> None:
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/ws/stream",
                headers={"Authorization": "Bearer wrong-token"},
            ) as ws:
                ws.receive_json()

    def test_websocket_wrong_query_token_closes_connection(self, app: FastAPI) -> None:
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream?token=wrong-token") as ws:
                ws.receive_json()


# ---------------------------------------------------------------------------
# Requirement 26.6: Operation NOT performed on auth failure
# ---------------------------------------------------------------------------


class TestOperationNotPerformed:
    """Verify that the protected operation is never reached on auth failure."""

    def test_action_not_performed_without_auth(self, client: TestClient) -> None:
        """POST /action must NOT return the action result without auth."""
        response = client.post("/action")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        # The action response body is not present
        assert "action" not in response.json()

    def test_action_not_performed_with_wrong_token(self, client: TestClient) -> None:
        """POST /action must NOT return the action result with wrong token."""
        response = client.post(
            "/action",
            headers={"Authorization": "Bearer invalid"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "action" not in response.json()


# ---------------------------------------------------------------------------
# Auth disabled mode (for testing/development)
# ---------------------------------------------------------------------------


class TestAuthDisabled:
    """Test that auth can be disabled for testing."""

    def test_auth_disabled_bypasses_validation(self, client: TestClient) -> None:
        set_auth_disabled(True)
        response = client.get("/protected")
        assert response.status_code == 200
        assert response.json()["token_used"] == "auth-disabled"

    def test_auth_disabled_allows_any_token(self, client: TestClient) -> None:
        set_auth_disabled(True)
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer anything"},
        )
        assert response.status_code == 200

    def test_auth_disabled_websocket_bypasses(self, app: FastAPI) -> None:
        set_auth_disabled(True)
        client = TestClient(app)
        with client.websocket_connect("/ws/stream") as ws:
            data = ws.receive_json()
            assert data["connected"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestAuthEdgeCases:
    """Edge cases for auth validation."""

    def test_no_configured_token_rejects_all(self, client: TestClient) -> None:
        """If FORGE_API_TOKEN is empty, all requests are rejected."""
        set_api_token("")
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer some-token"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_token_comparison_is_exact(self, client: TestClient) -> None:
        """Token must match exactly (no prefix/suffix tolerance)."""
        response = client.get(
            "/protected",
            headers={"Authorization": f"Bearer {TEST_TOKEN}extra"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_token_is_case_sensitive(self, client: TestClient) -> None:
        """Token comparison is case-sensitive."""
        response = client.get(
            "/protected",
            headers={"Authorization": f"Bearer {TEST_TOKEN.upper()}"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestHealthEndpointBypass:
    """Test that /health endpoint bypasses auth."""

    def test_health_endpoint_allows_no_auth(self, client: TestClient) -> None:
        """GET /health should work without auth token."""
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/health")
        async def health_endpoint(token: str = Depends(require_auth)):
            return {"status": "healthy"}

        # Create a test client for this specific app
        from fastapi.testclient import TestClient
        health_client = TestClient(app)
        
        response = health_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_health_endpoint_still_accepts_valid_token(self, client: TestClient) -> None:
        """GET /health should also work WITH auth token (backwards compatible)."""
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/health")
        async def health_endpoint(token: str = Depends(require_auth)):
            return {"status": "healthy"}

        from fastapi.testclient import TestClient
        health_client = TestClient(app)
        
        response = health_client.get(
            "/health",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
