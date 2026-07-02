"""Authentication and authorization for the Forge Application Layer.

Provides a FastAPI dependency that validates Bearer token credentials on all
REST endpoints and WebSocket connections. Rejects requests with missing or
invalid credentials with a 401 Unauthorized JSON response, ensuring the
requested operation is never performed on auth failure.

Requirements: 26.5, 26.6
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The expected Bearer token. Read from environment variable at import time.
# For testing, set FORGE_API_TOKEN to any non-empty string.
# If FORGE_AUTH_DISABLED is "true", auth is bypassed (test/dev only).
_AUTH_DISABLED = os.environ.get("FORGE_AUTH_DISABLED", "").lower() == "true"
_API_TOKEN: str = os.environ.get("FORGE_API_TOKEN", "")

# The security scheme (auto_error=False so we can produce a custom JSON body)
_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Helpers for getting request context
# ---------------------------------------------------------------------------

async def _get_request_opt(request: Request) -> Request | None:
    """Dependency that provides the current request object.
    
    Returns the Request if available, None otherwise.
    This is used to check the request path for health endpoint bypass.
    """
    return request


# ---------------------------------------------------------------------------
# Auth error response model
# ---------------------------------------------------------------------------

AUTH_ERROR_BODY = {
    "error": "authentication_failure",
    "detail": "Missing or invalid authentication credentials.",
}


# ---------------------------------------------------------------------------
# REST dependency
# ---------------------------------------------------------------------------


async def require_auth(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    request: Request | None = Depends(_get_request_opt),
) -> str:
    """FastAPI dependency that validates Bearer token on REST endpoints.

    Returns the validated token string on success.
    Raises HTTPException 401 with a JSON error body on failure.
    The operation is never reached if this dependency raises.

    Note: /health endpoint bypasses auth regardless of this dependency,
    since health checks must work without credentials.
    """
    if _AUTH_DISABLED:
        return "auth-disabled"

    # Allow unauthenticated access to health check endpoint
    if request is not None and request.url.path == "/health":
        return "health-bypass"

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTH_ERROR_BODY,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not _validate_token(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTH_ERROR_BODY,
            headers={"WWW-Authenticate": "Bearer"},
        )

    return credentials.credentials


# ---------------------------------------------------------------------------
# WebSocket dependency
# ---------------------------------------------------------------------------


class WebSocketAuthError(Exception):
    """Raised when WebSocket auth fails, signaling connection should be closed."""

    pass


async def require_ws_auth(websocket: WebSocket) -> str:
    """FastAPI dependency that validates Bearer token on WebSocket connections.

    Extracts the token from either:
    1. The ``Authorization`` header (if client supports it), or
    2. A ``token`` query parameter (common for browser WebSocket clients).

    Returns the validated token string on success.
    Raises WebSocketAuthError on failure (caller should close the connection).
    """
    if _AUTH_DISABLED:
        return "auth-disabled"

    token: str | None = None

    # Try Authorization header first
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    # Fall back to query parameter
    if not token:
        token = websocket.query_params.get("token")

    if not token or not _validate_token(token):
        raise WebSocketAuthError("Missing or invalid authentication credentials.")

    return token


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def _validate_token(token: str) -> bool:
    """Check token against the configured API token.

    Returns True if the token matches, False otherwise.
    An empty configured token means no valid token exists (all rejected).
    """
    if not _API_TOKEN:
        return False
    return token == _API_TOKEN


# ---------------------------------------------------------------------------
# Helpers for testing
# ---------------------------------------------------------------------------


def set_api_token(token: str) -> None:
    """Override the API token at runtime (for testing only)."""
    global _API_TOKEN
    _API_TOKEN = token


def set_auth_disabled(disabled: bool) -> None:
    """Enable or disable auth bypass at runtime (for testing only)."""
    global _AUTH_DISABLED
    _AUTH_DISABLED = disabled


def reset_auth_state() -> None:
    """Reset auth state to environment defaults (for test teardown)."""
    global _API_TOKEN, _AUTH_DISABLED
    _API_TOKEN = os.environ.get("FORGE_API_TOKEN", "")
    _AUTH_DISABLED = os.environ.get("FORGE_AUTH_DISABLED", "").lower() == "true"
