"""Unit tests for session store CRUD operations.

Tests create_session, get_session, list_sessions, update_session,
and delete_session functions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.db.session_store import (
    create_session,
    delete_session,
    get_session,
    list_sessions,
    update_session,
)

# ---------------------------------------------------------------------------
# create_session Tests
# ---------------------------------------------------------------------------


class TestCreateSession:
    """Tests for create_session function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.fixture
    def session_data(self) -> dict:
        """Sample session data for testing."""
        return {
            "repo_url": "https://github.com/user/repo",
            "goal": "Build a REST API",
            "build_mode": "new",
        }

    @pytest.mark.asyncio
    async def test_create_session_generates_uuid(
        self, mock_pool: MagicMock, session_data: dict
    ) -> None:
        """create_session generates a UUID if id not provided."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": UUID("550e8400-e29b-41d4-a716-446655440000"),
                "repo_url": session_data["repo_url"],
                "goal": session_data["goal"],
                "build_mode": session_data["build_mode"],
                "status": "pending",
            }
        )

        result = await create_session(pool=mock_pool, session_data=session_data)

        assert result["repo_url"] == session_data["repo_url"]
        assert result["goal"] == session_data["goal"]

    @pytest.mark.asyncio
    async def test_create_session_with_provided_id(
        self, mock_pool: MagicMock, session_data: dict
    ) -> None:
        """create_session uses provided id if given."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        session_data_with_id = {**session_data, "id": session_id}

        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": UUID(session_id),
                **session_data,
                "status": "pending",
            }
        )

        result = await create_session(pool=mock_pool, session_data=session_data_with_id)

        assert result["repo_url"] == session_data["repo_url"]

    @pytest.mark.asyncio
    async def test_create_session_sets_timestamps(
        self, mock_pool: MagicMock, session_data: dict
    ) -> None:
        """create_session sets created_at and updated_at."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": UUID("550e8400-e29b-41d4-a716-446655440000"),
                **session_data,
                "status": "pending",
                "created_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            }
        )

        result = await create_session(pool=mock_pool, session_data=session_data)

        assert "created_at" in result
        assert "updated_at" in result

    @pytest.mark.asyncio
    async def test_create_session_with_context_json(
        self, mock_pool: MagicMock, session_data: dict
    ) -> None:
        """create_session accepts optional context_json."""
        session_data_with_context = {
            **session_data,
            "context_json": {"tasks": ["task1", "task2"]},
        }

        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": UUID("550e8400-e29b-41d4-a716-446655440000"),
                **session_data_with_context,
                "status": "pending",
            }
        )

        result = await create_session(pool=mock_pool, session_data=session_data_with_context)

        assert "context_json" in result


# ---------------------------------------------------------------------------
# get_session Tests
# ---------------------------------------------------------------------------


class TestGetSession:
    """Tests for get_session function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.mark.asyncio
    async def test_get_session_found(self, mock_pool: MagicMock) -> None:
        """get_session returns session dict when found."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": UUID(session_id),
                "repo_url": "https://github.com/user/repo",
                "goal": "Test goal",
                "build_mode": "new",
                "status": "pending",
            }
        )

        result = await get_session(pool=mock_pool, session_id=session_id)

        assert result is not None
        assert result["repo_url"] == "https://github.com/user/repo"

    @pytest.mark.asyncio
    async def test_get_session_not_found(self, mock_pool: MagicMock) -> None:
        """get_session returns None when session not found."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value=None
        )

        result = await get_session(pool=mock_pool, session_id=session_id)

        assert result is None


# ---------------------------------------------------------------------------
# list_sessions Tests
# ---------------------------------------------------------------------------


class TestListSessions:
    """Tests for list_sessions function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.mark.asyncio
    async def test_list_sessions_returns_all(self, mock_pool: MagicMock) -> None:
        """list_sessions returns all sessions."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[
                {"id": UUID("550e8400-e29b-41d4-a716-446655440000"), "goal": "Goal 1"},
                {"id": UUID("660e8400-e29b-41d4-a716-446655440001"), "goal": "Goal 2"},
            ]
        )

        result = await list_sessions(pool=mock_pool)

        assert len(result) == 2
        assert result[0]["goal"] == "Goal 1"
        assert result[1]["goal"] == "Goal 2"

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, mock_pool: MagicMock) -> None:
        """list_sessions returns empty list when no sessions."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[]
        )

        result = await list_sessions(pool=mock_pool)

        assert result == []

    @pytest.mark.asyncio
    async def test_list_sessions_orders_by_created_at_desc(
        self, mock_pool: MagicMock
    ) -> None:
        """list_sessions orders by created_at DESC (newest first)."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[]
        )

        await list_sessions(pool=mock_pool)

        call_args = mock_pool.acquire.return_value.__aenter__.return_value.fetch.call_args
        query = call_args[0][0]
        assert "ORDER BY created_at DESC" in query


# ---------------------------------------------------------------------------
# update_session Tests
# ---------------------------------------------------------------------------


class TestUpdateSession:
    """Tests for update_session function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.mark.asyncio
    async def test_update_session_allowed_fields(
        self, mock_pool: MagicMock
    ) -> None:
        """update_session only updates allowed fields."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        updates = {
            "goal": "Updated goal",
            "status": "completed",
            "repo_url": "https://github.com/other/repo",
            "custom_field": "not_allowed",  # Should be filtered out
        }

        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": UUID(session_id),
                "goal": "Updated goal",
                "status": "completed",
                "repo_url": "https://github.com/other/repo",
            }
        )

        await update_session(pool=mock_pool, session_id=session_id, updates=updates)

        call_args = mock_pool.acquire.return_value.__aenter__.return_value.fetchrow.call_args
        query = call_args[0][0]
        params = call_args[0][1:]

        # Verify allowed fields are in the query
        assert "goal" in query
        assert "status" in query
        assert "repo_url" in query
        # custom_field should not be in params
        assert "not_allowed" not in str(params)

    @pytest.mark.asyncio
    async def test_update_session_updates_timestamp(
        self, mock_pool: MagicMock
    ) -> None:
        """update_session always updates updated_at."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        updates = {"goal": "New goal"}

        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": UUID(session_id),
                "goal": "New goal",
                "updated_at": datetime.now(timezone.utc),
            }
        )

        await update_session(pool=mock_pool, session_id=session_id, updates=updates)

        call_args = mock_pool.acquire.return_value.__aenter__.return_value.fetchrow.call_args
        query = call_args[0][0]
        assert "updated_at" in query

    @pytest.mark.asyncio
    async def test_update_session_empty_updates_returns_current(
        self, mock_pool: MagicMock
    ) -> None:
        """update_session with empty updates returns current state."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        current_session = {
            "id": UUID(session_id),
            "goal": "Current goal",
            "status": "pending",
        }

        # Mock get_session result
        with patch("app.db.session_store.get_session", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = current_session
            result = await update_session(
                pool=mock_pool, session_id=session_id, updates={}
            )

            assert result["goal"] == "Current goal"
            # Should not have executed UPDATE
            mock_pool.acquire.return_value.__aenter__.return_value.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_session_not_found_raises(self, mock_pool: MagicMock) -> None:
        """update_session raises ValueError when session not found."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value=None
        )

        with pytest.raises(ValueError, match="not found"):
            await update_session(
                pool=mock_pool, session_id=session_id, updates={"goal": "New"}
            )


# ---------------------------------------------------------------------------
# delete_session Tests
# ---------------------------------------------------------------------------


class TestDeleteSession:
    """Tests for delete_session function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.mark.asyncio
    async def test_delete_session_success(self, mock_pool: MagicMock) -> None:
        """delete_session returns True when session is deleted."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        mock_pool.acquire.return_value.__aenter__.return_value.execute = AsyncMock(
            return_value="DELETE 1"
        )

        result = await delete_session(pool=mock_pool, session_id=session_id)

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_session_not_found(self, mock_pool: MagicMock) -> None:
        """delete_session returns False when session not found."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        mock_pool.acquire.return_value.__aenter__.return_value.execute = AsyncMock(
            return_value="DELETE 0"
        )

        result = await delete_session(pool=mock_pool, session_id=session_id)

        assert result is False
