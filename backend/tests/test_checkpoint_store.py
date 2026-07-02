"""Regression tests for app.db.checkpoint_store's non-UUID session_id handling.

Reproduces the bug from live session e2e-sandbox-final: a client-supplied
session_id that is not a valid UUID string (e.g. "e2e-sandbox-final") caused
`uuid.UUID(session_id)` to raise `ValueError: badly formed hexadecimal UUID
string` inside write_checkpoint/get_latest_checkpoint, which the calling
CheckpointMiddleware caught and logged repeatedly as "Checkpoint failed for
session ...".

These tests assert the fixed behavior: a non-UUID session_id causes the
checkpoint functions to skip the database call and return None (no
exception), rather than raising.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db import checkpoint_store


class _FakeConnection:
    """Minimal fake asyncpg connection recording whether a query was issued."""

    def __init__(self) -> None:
        self.execute = AsyncMock(return_value=None)
        self.fetchrow = AsyncMock(return_value=None)


class _FakePool:
    """Minimal fake asyncpg pool exposing an async context-managed acquire()."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def acquire(self):
        conn = self._connection

        class _AcquireCtx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc_info):
                return False

        return _AcquireCtx()


class TestWriteCheckpointNonUuidSessionId:
    """write_checkpoint must not raise for a non-UUID session_id."""

    @pytest.mark.asyncio
    async def test_non_uuid_session_id_does_not_raise(self) -> None:
        """A real-world non-UUID session_id (as used by /workflow/invoke clients)
        must not raise ValueError — this is the exact bug from session
        e2e-sandbox-final ("badly formed hexadecimal UUID string").
        """
        conn = _FakeConnection()
        pool = _FakePool(conn)

        # Must not raise.
        await checkpoint_store.write_checkpoint(
            pool, "e2e-sandbox-final", "execute", 1, {"foo": "bar"}
        )

    @pytest.mark.asyncio
    async def test_non_uuid_session_id_skips_db_call(self) -> None:
        """The database should never be queried for a session_id that can't
        possibly match the sessions.id foreign key.
        """
        conn = _FakeConnection()
        pool = _FakePool(conn)

        await checkpoint_store.write_checkpoint(
            pool, "not-a-uuid", "execute", 1, {"foo": "bar"}
        )

        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_uuid_session_id_still_writes(self) -> None:
        """A genuinely valid UUID session_id must still reach the database —
        the fix must not silently no-op valid input.
        """
        conn = _FakeConnection()
        pool = _FakePool(conn)
        real_session_id = str(uuid.uuid4())

        await checkpoint_store.write_checkpoint(
            pool, real_session_id, "execute", 1, {"foo": "bar"}
        )

        conn.execute.assert_called_once()
        # The first positional query param (after the SQL string) must be a
        # parsed uuid.UUID, not the raw string.
        call_args = conn.execute.call_args
        assert isinstance(call_args.args[1], uuid.UUID)
        assert call_args.args[1] == uuid.UUID(real_session_id)


class TestGetLatestCheckpointNonUuidSessionId:
    """get_latest_checkpoint must not raise for a non-UUID session_id."""

    @pytest.mark.asyncio
    async def test_non_uuid_session_id_returns_none(self) -> None:
        conn = _FakeConnection()
        pool = _FakePool(conn)

        result = await checkpoint_store.get_latest_checkpoint(
            pool, "e2e-sandbox-final"
        )

        assert result is None
        conn.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_uuid_session_id_still_queries(self) -> None:
        conn = _FakeConnection()
        pool = _FakePool(conn)
        real_session_id = str(uuid.uuid4())

        await checkpoint_store.get_latest_checkpoint(pool, real_session_id)

        conn.fetchrow.assert_called_once()
