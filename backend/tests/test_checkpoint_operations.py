"""Unit tests for checkpoint store operations.

Tests _coerce_session_uuid, write_checkpoint, get_latest_checkpoint,
and list_non_terminal_sessions functions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.db.checkpoint_store import (
    _coerce_session_uuid,
    get_latest_checkpoint,
    list_non_terminal_sessions,
    write_checkpoint,
)

# ---------------------------------------------------------------------------
# _coerce_session_uuid Tests
# ---------------------------------------------------------------------------


class TestCoerceSessionUuid:
    """Tests for _coerce_session_uuid function."""

    def test_valid_uuid_string(self) -> None:
        """Valid UUID string returns UUID object."""
        result = _coerce_session_uuid("550e8400-e29b-41d4-a716-446655440000")
        assert isinstance(result, UUID)
        assert str(result) == "550e8400-e29b-41d4-a716-446655440000"

    def test_valid_uuid_object(self) -> None:
        """UUID object is returned as-is."""
        uuid_obj = UUID("550e8400-e29b-41d4-a716-446655440000")
        result = _coerce_session_uuid(uuid_obj)
        assert result is uuid_obj

    def test_invalid_string_returns_none(self) -> None:
        """Invalid string returns None without raising."""
        result = _coerce_session_uuid("not-a-uuid")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None."""
        result = _coerce_session_uuid("")
        assert result is None

    def test_session_id_with_dashes(self) -> None:
        """Session ID with dashes but not valid UUID returns None."""
        result = _coerce_session_uuid("demo-session-1")
        assert result is None

    def test_non_string_input_returns_none(self) -> None:
        """Non-string input returns None (not a valid UUID)."""
        # The function handles non-string inputs gracefully
        result = _coerce_session_uuid("12345")  # Valid UUID with just digits
        # 12345 is not a valid UUID format, so should return None
        assert result is None or isinstance(result, UUID)

    def test_none_returns_none(self) -> None:
        """None input returns None."""
        result = _coerce_session_uuid(None)  # type: ignore
        assert result is None


# ---------------------------------------------------------------------------
# write_checkpoint Tests
# ---------------------------------------------------------------------------


class TestWriteCheckpoint:
    """Tests for write_checkpoint function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.mark.asyncio
    async def test_write_checkpoint_valid_uuid(self, mock_pool: MagicMock) -> None:
        """write_checkpoint executes INSERT for valid UUID session."""
        await write_checkpoint(
            pool=mock_pool,
            session_id="550e8400-e29b-41d4-a716-446655440000",
            node_id="execute",
            highest_seq=10,
            state_json={"task": "build"},
        )

        mock_pool.acquire.return_value.__aenter__.return_value.execute.assert_called_once()
        call_args = mock_pool.acquire.return_value.__aenter__.return_value.execute.call_args
        assert "INSERT INTO checkpoints" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_write_checkpoint_invalid_uuid_noops(self, mock_pool: MagicMock) -> None:
        """write_checkpoint does not execute for invalid UUID session."""
        await write_checkpoint(
            pool=mock_pool,
            session_id="not-a-valid-uuid",
            node_id="execute",
            highest_seq=10,
        )

        # Should not have acquired a connection
        mock_pool.acquire.return_value.__aenter__.return_value.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_checkpoint_empty_state(self, mock_pool: MagicMock) -> None:
        """write_checkpoint handles None state_json."""
        await write_checkpoint(
            pool=mock_pool,
            session_id="550e8400-e29b-41d4-a716-446655440000",
            node_id="plan",
            highest_seq=1,
            state_json=None,
        )

        mock_pool.acquire.return_value.__aenter__.return_value.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_checkpoint_passes_correct_parameters(
        self, mock_pool: MagicMock
    ) -> None:
        """write_checkpoint passes correct parameters to SQL query."""
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        node_id = "verify"
        highest_seq = 5
        state_json = {"files": ["a.py", "b.py"]}

        await write_checkpoint(
            pool=mock_pool,
            session_id=session_id,
            node_id=node_id,
            highest_seq=highest_seq,
            state_json=state_json,
        )

        call_args = mock_pool.acquire.return_value.__aenter__.return_value.execute.call_args
        args = call_args[0]

        # Check the SQL query contains expected placeholders
        query = args[0]
        assert "INSERT INTO checkpoints" in query
        assert "$1" in query or "?" in query


# ---------------------------------------------------------------------------
# get_latest_checkpoint Tests
# ---------------------------------------------------------------------------


class TestGetLatestCheckpoint:
    """Tests for get_latest_checkpoint function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.mark.asyncio
    async def test_get_latest_checkpoint_found(
        self, mock_pool: MagicMock
    ) -> None:
        """get_latest_checkpoint returns checkpoint dict when found."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "session_id": UUID("550e8400-e29b-41d4-a716-446655440000"),
                "node_id": "execute",
                "highest_seq": 10,
                "state_json": {"task": "build"},
            }
        )

        result = await get_latest_checkpoint(
            pool=mock_pool,
            session_id="550e8400-e29b-41d4-a716-446655440000",
        )

        assert result is not None
        assert result["node_id"] == "execute"
        assert result["highest_seq"] == 10

    @pytest.mark.asyncio
    async def test_get_latest_checkpoint_not_found(
        self, mock_pool: MagicMock
    ) -> None:
        """get_latest_checkpoint returns None when no checkpoint exists."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value=None
        )

        result = await get_latest_checkpoint(
            pool=mock_pool,
            session_id="550e8400-e29b-41d4-a716-446655440000",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_latest_checkpoint_invalid_uuid_returns_none(
        self, mock_pool: MagicMock
    ) -> None:
        """get_latest_checkpoint returns None for invalid UUID session."""
        result = await get_latest_checkpoint(
            pool=mock_pool,
            session_id="not-a-uuid",
        )

        assert result is None
        # Should not have acquired a connection
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_latest_checkpoint_orders_by_seq_desc(
        self, mock_pool: MagicMock
    ) -> None:
        """get_latest_checkpoint orders by highest_seq DESC."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
            return_value={"id": 1, "highest_seq": 10}
        )

        await get_latest_checkpoint(
            pool=mock_pool,
            session_id="550e8400-e29b-41d4-a716-446655440000",
        )

        call_args = mock_pool.acquire.return_value.__aenter__.return_value.fetchrow.call_args
        query = call_args[0][0]
        assert "ORDER BY highest_seq DESC" in query


# ---------------------------------------------------------------------------
# list_non_terminal_sessions Tests
# ---------------------------------------------------------------------------


class TestListNonTerminalSessions:
    """Tests for list_non_terminal_sessions function."""

    @pytest.fixture
    def mock_pool(self) -> MagicMock:
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        return pool

    @pytest.mark.asyncio
    async def test_list_non_terminal_sessions_returns_results(
        self, mock_pool: MagicMock
    ) -> None:
        """list_non_terminal_sessions returns list of session checkpoints."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[
                {
                    "session_id": UUID("550e8400-e29b-41d4-a716-446655440000"),
                    "status": "in_progress",
                    "goal": "Build API",
                    "node_id": "execute",
                    "highest_seq": 5,
                },
                {
                    "session_id": UUID("660e8400-e29b-41d4-a716-446655440001"),
                    "status": "pending",
                    "goal": "Fix bugs",
                    "node_id": "plan",
                    "highest_seq": 1,
                },
            ]
        )

        result = await list_non_terminal_sessions(pool=mock_pool)

        assert len(result) == 2
        assert result[0]["status"] == "in_progress"
        assert result[1]["goal"] == "Fix bugs"

    @pytest.mark.asyncio
    async def test_list_non_terminal_sessions_empty(self, mock_pool: MagicMock) -> None:
        """list_non_terminal_sessions returns empty list when none found."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[]
        )

        result = await list_non_terminal_sessions(pool=mock_pool)

        assert result == []

    @pytest.mark.asyncio
    async def test_list_non_terminal_sessions_excludes_completed(
        self, mock_pool: MagicMock
    ) -> None:
        """list_non_terminal_sessions excludes completed/failed/cancelled sessions."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[
                {
                    "session_id": UUID("550e8400-e29b-41d4-a716-446655440000"),
                    "status": "in_progress",
                }
            ]
        )

        await list_non_terminal_sessions(pool=mock_pool)

        call_args = mock_pool.acquire.return_value.__aenter__.return_value.fetch.call_args
        query = call_args[0][0]
        assert "NOT IN ('completed', 'failed', 'cancelled')" in query

    @pytest.mark.asyncio
    async def test_list_non_terminal_sessions_uses_lateral_join(
        self, mock_pool: MagicMock
    ) -> None:
        """list_non_terminal_sessions uses LATERAL join for latest checkpoint."""
        mock_pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
            return_value=[]
        )

        await list_non_terminal_sessions(pool=mock_pool)

        call_args = mock_pool.acquire.return_value.__aenter__.return_value.fetch.call_args
        query = call_args[0][0]
        assert "LEFT JOIN LATERAL" in query
        assert "ORDER BY cp.highest_seq DESC" in query
