"""Session lifecycle management for the Forge Runtime.

Implements session CRUD operations: create, list, get, delete. Each session is
tied to a repository URL, a plain-English goal, and a Build_Mode. VCS tokens
are stored in the SecretHolder (never in session state); only a token reference
is persisted.

For now, persistence uses an in-memory dict. The relational store can be wired
later behind the same interface.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.runtime.models import BuildMode
from app.runtime.secrets import SecretHolder, TokenReference

logger = logging.getLogger(__name__)


class SessionNotFoundError(Exception):
    """Raised when a requested session identifier does not exist.

    Per requirement 1.6: return a not-found response with the requested
    identifier and do not modify any persisted state.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")


class SessionValidationError(Exception):
    """Raised when session creation request has invalid inputs.

    Per requirement 1.7: reject the request with a validation error
    identifying the invalid field and do not create a session.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"Validation error on '{field}': {reason}")


@dataclass
class Session:
    """Represents a build session in the Forge Runtime.

    Each session is tied to a repository URL, a goal, and a Build_Mode.
    The VCS token is stored only in the SecretHolder; the session state
    holds a non-reversible TokenReference.
    """

    id: str
    repo_url: str
    goal: str
    build_mode: BuildMode
    status: str = "created"
    token_reference: TokenReference | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    workspaces: list[str] = field(default_factory=list)
    build_in_progress: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _validate_repo_url(repo_url: str) -> None:
    """Validate that a repository URL is non-empty and well-formed.

    A well-formed URL must have a scheme (http or https) and a network location (host).

    Args:
        repo_url: The repository URL to validate.

    Raises:
        SessionValidationError: If the URL is empty or malformed.
    """
    if not repo_url or not repo_url.strip():
        raise SessionValidationError("repo_url", "Repository URL must not be empty")

    parsed = urlparse(repo_url.strip())

    if parsed.scheme not in ("http", "https"):
        raise SessionValidationError(
            "repo_url",
            "Repository URL must use http or https scheme",
        )

    if not parsed.netloc:
        raise SessionValidationError(
            "repo_url",
            "Repository URL must contain a valid host",
        )


def _validate_goal(goal: str) -> None:
    """Validate that a goal is non-empty.

    Args:
        goal: The plain-English goal string.

    Raises:
        SessionValidationError: If the goal is empty.
    """
    if not goal or not goal.strip():
        raise SessionValidationError("goal", "Goal must not be empty")


class SessionManager:
    """Manages session lifecycle: create, list, get, delete.

    Uses an in-memory dict as the persistence layer. The relational store
    can be wired later behind the same interface.

    Collaborates with:
    - SecretHolder: stores VCS tokens per session (never in session state)
    - WorkspaceManager (future): destroys workspaces on session deletion
    """

    def __init__(self, secret_holder: SecretHolder | None = None) -> None:
        """Initialize the SessionManager.

        Args:
            secret_holder: The SecretHolder for VCS token storage.
                           If None, a new instance is created.
        """
        self._sessions: dict[str, Session] = {}
        self._secret_holder = secret_holder or SecretHolder()

    @property
    def secret_holder(self) -> SecretHolder:
        """The SecretHolder used for VCS token storage."""
        return self._secret_holder

    def create_session(
        self,
        *,
        repo_url: str,
        goal: str,
        build_mode: BuildMode | str = BuildMode.NEW,
        vcs_token: str = "",
    ) -> Session:
        """Create a new session with a unique ID, initial status, and Build_Mode.

        Validates inputs, stores the VCS token in the SecretHolder, and persists
        only a token reference in the session state.

        Args:
            repo_url: The repository URL for this build session.
            goal: The plain-English goal describing what to build.
            build_mode: The kind of build (new, extend, analyze, document).
            vcs_token: The VCS access token (stored in SecretHolder, not in session).

        Returns:
            The newly created Session.

        Raises:
            SessionValidationError: If repo_url is empty/malformed or goal is empty.
        """
        # Validate inputs (Requirement 1.7)
        _validate_repo_url(repo_url)
        _validate_goal(goal)

        # Normalize build_mode
        if isinstance(build_mode, str):
            try:
                build_mode = BuildMode(build_mode)
            except ValueError:
                raise SessionValidationError(
                    "build_mode",
                    f"Invalid build mode: {build_mode}. Must be one of: new, extend, analyze, document",
                )

        # Generate unique session ID (Requirement 1.1)
        session_id = str(uuid.uuid4())

        # Store VCS token in SecretHolder (Requirement 1.2)
        token_ref: TokenReference | None = None
        if vcs_token:
            self._secret_holder.store_secret(session_id, "vcs_token", vcs_token)
            token_ref = self._secret_holder.get_token_reference(session_id, "vcs_token")

        # Create session with initial status "created" (Requirement 1.1)
        session = Session(
            id=session_id,
            repo_url=repo_url.strip(),
            goal=goal.strip(),
            build_mode=build_mode,
            status="created",
            token_reference=token_ref,
        )

        # Persist session (in-memory for now)
        self._sessions[session_id] = session

        logger.info(
            "Session created: id=%s, repo_url=%s, build_mode=%s",
            session_id,
            repo_url,
            build_mode.value,
        )

        return session

    def list_sessions(self) -> list[Session]:
        """List all sessions with their identifiers, repo URLs, build modes, and statuses.

        Per requirement 1.3: returns all sessions, or an empty list when none exist.

        Returns:
            List of all sessions.
        """
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> Session:
        """Get a session by its identifier.

        Per requirement 1.4: returns the session's detail including status
        and operability.

        Args:
            session_id: The session identifier to look up.

        Returns:
            The Session object.

        Raises:
            SessionNotFoundError: If the session does not exist (Requirement 1.6).
        """
        if session_id not in self._sessions:
            raise SessionNotFoundError(session_id)
        return self._sessions[session_id]

    def delete_session(self, session_id: str) -> None:
        """Delete a session, cleaning up all associated resources.

        Per requirements 1.5 and 1.8:
        - Stop in-progress build if running
        - Destroy all workspaces associated with the session
        - Clear secrets from SecretHolder
        - Remove session from persistence

        Args:
            session_id: The session identifier to delete.

        Raises:
            SessionNotFoundError: If the session does not exist (Requirement 1.6).
        """
        if session_id not in self._sessions:
            raise SessionNotFoundError(session_id)

        session = self._sessions[session_id]

        # Stop in-progress build (Requirement 1.8)
        if session.build_in_progress:
            logger.info("Stopping in-progress build for session %s", session_id)
            session.build_in_progress = False
            session.status = "stopped"

        # Destroy workspaces (Requirement 1.5)
        if session.workspaces:
            logger.info(
                "Destroying %d workspaces for session %s",
                len(session.workspaces),
                session_id,
            )
            session.workspaces.clear()

        # Clear secrets from SecretHolder (Requirement 22.6)
        self._secret_holder.clear_session(session_id)

        # Remove session from persistence
        del self._sessions[session_id]

        logger.info("Session deleted: %s", session_id)

    def session_count(self) -> int:
        """Return the number of active sessions."""
        return len(self._sessions)

    def has_session(self, session_id: str) -> bool:
        """Check whether a session exists."""
        return session_id in self._sessions
