"""Unit tests for Session Lifecycle Management.

Tests session CRUD operations: create, list, get, delete with validation,
secret handling, and error cases.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
"""

from __future__ import annotations

import uuid

import pytest

from app.runtime.models import BuildMode
from app.runtime.secrets import SecretHolder, TokenReference
from app.runtime.session import (
    Session,
    SessionManager,
    SessionNotFoundError,
    SessionValidationError,
)


@pytest.fixture
def secret_holder() -> SecretHolder:
    """A fresh SecretHolder for testing."""
    return SecretHolder()


@pytest.fixture
def manager(secret_holder: SecretHolder) -> SessionManager:
    """A SessionManager with an injected SecretHolder."""
    return SessionManager(secret_holder=secret_holder)


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 1.1: Create session with unique ID, initial status, Build_Mode
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionCreate:
    """Test session creation with valid inputs."""

    def test_create_session_returns_session(self, manager: SessionManager) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Add authentication",
            build_mode=BuildMode.NEW,
            vcs_token="ghp_test123",
        )
        assert isinstance(session, Session)

    def test_create_session_has_unique_id(self, manager: SessionManager) -> None:
        s1 = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Add auth",
            vcs_token="ghp_token1",
        )
        s2 = manager.create_session(
            repo_url="https://github.com/user/repo2",
            goal="Add logging",
            vcs_token="ghp_token2",
        )
        assert s1.id != s2.id
        # Verify IDs are valid UUIDs
        uuid.UUID(s1.id)
        uuid.UUID(s2.id)

    def test_create_session_initial_status_is_created(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        assert session.status == "created"

    def test_create_session_assigns_build_mode(self, manager: SessionManager) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Analyze codebase",
            build_mode=BuildMode.ANALYZE,
            vcs_token="ghp_abc",
        )
        assert session.build_mode == BuildMode.ANALYZE

    def test_create_session_default_build_mode_is_new(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build something",
            vcs_token="ghp_abc",
        )
        assert session.build_mode == BuildMode.NEW

    def test_create_session_accepts_string_build_mode(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Extend feature",
            build_mode="extend",
            vcs_token="ghp_abc",
        )
        assert session.build_mode == BuildMode.EXTEND

    def test_create_session_stores_repo_url_and_goal(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Add new feature",
            vcs_token="ghp_abc",
        )
        assert session.repo_url == "https://github.com/user/repo"
        assert session.goal == "Add new feature"

    def test_create_session_strips_whitespace(self, manager: SessionManager) -> None:
        session = manager.create_session(
            repo_url="  https://github.com/user/repo  ",
            goal="  Build feature  ",
            vcs_token="ghp_abc",
        )
        assert session.repo_url == "https://github.com/user/repo"
        assert session.goal == "Build feature"

    def test_create_all_build_modes(self, manager: SessionManager) -> None:
        for mode in BuildMode:
            session = manager.create_session(
                repo_url="https://github.com/user/repo",
                goal=f"Test {mode.value}",
                build_mode=mode,
                vcs_token="ghp_abc",
            )
            assert session.build_mode == mode


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 1.2: Store VCS token in SecretHolder, token reference in state
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionSecretHandling:
    """Test VCS token storage in SecretHolder."""

    def test_vcs_token_stored_in_secret_holder(
        self, manager: SessionManager, secret_holder: SecretHolder
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_supersecret123",
        )
        # Token is retrievable from SecretHolder
        assert secret_holder.get_secret(session.id, "vcs_token") == "ghp_supersecret123"

    def test_session_state_has_token_reference_not_raw_token(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_supersecret123",
        )
        # Session state holds a TokenReference, not the raw token
        assert session.token_reference is not None
        assert isinstance(session.token_reference, TokenReference)
        assert "ghp_supersecret123" not in str(session.token_reference)

    def test_token_reference_contains_session_id(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        assert session.token_reference is not None
        assert session.token_reference.session_id == session.id
        assert session.token_reference.key_name == "vcs_token"

    def test_create_without_vcs_token(self, manager: SessionManager) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Analyze codebase",
            build_mode=BuildMode.ANALYZE,
        )
        # No token stored — reference is None
        assert session.token_reference is None


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 1.3: List sessions
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionList:
    """Test listing sessions."""

    def test_list_sessions_empty(self, manager: SessionManager) -> None:
        result = manager.list_sessions()
        assert result == []

    def test_list_sessions_returns_all(self, manager: SessionManager) -> None:
        manager.create_session(
            repo_url="https://github.com/user/repo1",
            goal="Goal 1",
            vcs_token="t1",
        )
        manager.create_session(
            repo_url="https://github.com/user/repo2",
            goal="Goal 2",
            build_mode=BuildMode.EXTEND,
            vcs_token="t2",
        )
        result = manager.list_sessions()
        assert len(result) == 2

    def test_list_sessions_contains_key_properties(
        self, manager: SessionManager
    ) -> None:
        manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="My goal",
            build_mode=BuildMode.DOCUMENT,
            vcs_token="t1",
        )
        sessions = manager.list_sessions()
        s = sessions[0]
        # Verify key properties are present
        assert s.id
        assert s.repo_url == "https://github.com/user/repo"
        assert s.build_mode == BuildMode.DOCUMENT
        assert s.status == "created"


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 1.4: Get session by ID
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionGet:
    """Test getting a session by identifier."""

    def test_get_session_returns_correct_session(
        self, manager: SessionManager
    ) -> None:
        created = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        fetched = manager.get_session(created.id)
        assert fetched.id == created.id
        assert fetched.repo_url == created.repo_url
        assert fetched.goal == created.goal
        assert fetched.build_mode == created.build_mode
        assert fetched.status == created.status

    def test_get_session_includes_status(self, manager: SessionManager) -> None:
        created = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        fetched = manager.get_session(created.id)
        assert fetched.status == "created"


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 1.5 + 1.8: Delete session
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionDelete:
    """Test session deletion including cleanup."""

    def test_delete_session_removes_it(self, manager: SessionManager) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        manager.delete_session(session.id)
        assert not manager.has_session(session.id)

    def test_delete_session_clears_secrets(
        self, manager: SessionManager, secret_holder: SecretHolder
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_secret_value",
        )
        # Verify token is stored
        assert secret_holder.get_secret(session.id, "vcs_token") == "ghp_secret_value"

        manager.delete_session(session.id)

        # Verify token is cleared
        assert secret_holder.get_secret(session.id, "vcs_token") is None

    def test_delete_session_destroys_workspaces(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        # Simulate workspaces being associated
        session.workspaces = ["ws-1", "ws-2", "ws-3"]

        manager.delete_session(session.id)

        # Session is gone; workspaces list was cleared before removal
        assert not manager.has_session(session.id)

    def test_delete_session_stops_in_progress_build(
        self, manager: SessionManager
    ) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        # Simulate in-progress build
        session.build_in_progress = True
        session.status = "building"

        manager.delete_session(session.id)

        # Session is removed (build was stopped before deletion)
        assert not manager.has_session(session.id)

    def test_delete_reduces_session_count(self, manager: SessionManager) -> None:
        s1 = manager.create_session(
            repo_url="https://github.com/user/repo1",
            goal="Goal 1",
            vcs_token="t1",
        )
        manager.create_session(
            repo_url="https://github.com/user/repo2",
            goal="Goal 2",
            vcs_token="t2",
        )
        assert manager.session_count() == 2
        manager.delete_session(s1.id)
        assert manager.session_count() == 1


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 1.6: Not-found for non-existent session IDs
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionNotFound:
    """Test not-found behavior for non-existent session IDs."""

    def test_get_nonexistent_session_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionNotFoundError) as exc_info:
            manager.get_session("nonexistent-id")
        assert exc_info.value.session_id == "nonexistent-id"

    def test_delete_nonexistent_session_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionNotFoundError) as exc_info:
            manager.delete_session("nonexistent-id")
        assert exc_info.value.session_id == "nonexistent-id"

    def test_get_after_delete_raises(self, manager: SessionManager) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        manager.delete_session(session.id)
        with pytest.raises(SessionNotFoundError):
            manager.get_session(session.id)

    def test_double_delete_raises(self, manager: SessionManager) -> None:
        session = manager.create_session(
            repo_url="https://github.com/user/repo",
            goal="Build feature",
            vcs_token="ghp_abc",
        )
        manager.delete_session(session.id)
        with pytest.raises(SessionNotFoundError):
            manager.delete_session(session.id)


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 1.7: Reject creation on invalid inputs
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionValidation:
    """Test validation error on invalid session creation inputs."""

    def test_empty_repo_url_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="",
                goal="Build feature",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "repo_url"

    def test_whitespace_only_repo_url_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="   ",
                goal="Build feature",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "repo_url"

    def test_malformed_repo_url_no_scheme_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="github.com/user/repo",
                goal="Build feature",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "repo_url"

    def test_malformed_repo_url_wrong_scheme_raises(
        self, manager: SessionManager
    ) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="ftp://github.com/user/repo",
                goal="Build feature",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "repo_url"

    def test_malformed_repo_url_no_host_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="https://",
                goal="Build feature",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "repo_url"

    def test_empty_goal_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="https://github.com/user/repo",
                goal="",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "goal"

    def test_whitespace_only_goal_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="https://github.com/user/repo",
                goal="   ",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "goal"

    def test_invalid_build_mode_string_raises(self, manager: SessionManager) -> None:
        with pytest.raises(SessionValidationError) as exc_info:
            manager.create_session(
                repo_url="https://github.com/user/repo",
                goal="Build feature",
                build_mode="invalid_mode",
                vcs_token="ghp_abc",
            )
        assert exc_info.value.field == "build_mode"

    def test_no_session_created_on_validation_error(
        self, manager: SessionManager
    ) -> None:
        """Verify that no session is created when validation fails."""
        try:
            manager.create_session(
                repo_url="",
                goal="Build feature",
                vcs_token="ghp_abc",
            )
        except SessionValidationError:
            pass
        assert manager.session_count() == 0
