"""PostgreSQL Persistence Layer for Forge Runtime.

This module provides database-backed implementations of the runtime interfaces,
replacing the in-memory defaults. It is automatically wired when DATABASE_URL
is available in the environment.

Requirements: All PostgreSQL stores must be ready to use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.pool import Pool

logger = logging.getLogger(__name__)

# Module-level pool reference
_pool: Any = None


async def init_persistence(database_url: str, pool_size: int = 10) -> bool:
    """Initialize the PostgreSQL connection pool.

    Args:
        database_url: The database connection URL.
        pool_size: Maximum number of connections in the pool.

    Returns:
        True if pool was created, False if DATABASE_URL not set.
    """
    global _pool

    if not database_url:
        logger.warning("DATABASE_URL not set — using in-memory stores")
        return False

    try:
        from app.db.pool import create_pool
        import os

        os.environ["DATABASE_URL"] = database_url
        os.environ["DATABASE_POOL_SIZE"] = str(pool_size)
        _pool = await create_pool()
        logger.info("PostgreSQL persistence layer initialized")
        return True
    except Exception as exc:
        logger.warning("Failed to initialize PostgreSQL pool: %s — using in-memory stores", exc)
        return False


async def get_pool() -> Any:
    """Get the database pool."""
    from app.db.pool import get_pool as _get_pool
    return await _get_pool()


async def close_persistence() -> None:
    """Close the database connection pool."""
    global _pool
    if _pool is not None:
        from app.db.pool import close_pool
        await close_pool()
        _pool = None
        logger.info("PostgreSQL persistence layer closed")


class PostgresSessionStore:
    """PostgreSQL-backed session store adapter.

    Wraps the db/session_store functions to match the SessionManager interface.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._in_memory: dict[str, Any] = {}
        self._use_db = True

    async def create(self, session_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new session in PostgreSQL."""
        if not self._use_db:
            session_id = session_data.get("id") or session_data.get("session_id")
            if session_id:
                self._in_memory[session_id] = session_data
            return session_data

        from app.db import session_store
        return await session_store.create_session(self._pool, session_data)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """Get a session by ID from PostgreSQL."""
        if not self._use_db:
            return self._in_memory.get(session_id)

        from app.db import session_store
        return await session_store.get_session(self._pool, session_id)

    async def list_all(self) -> list[dict[str, Any]]:
        """List all sessions from PostgreSQL."""
        if not self._use_db:
            return list(self._in_memory.values())

        from app.db import session_store
        return await session_store.list_sessions(self._pool)

    async def update(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update a session in PostgreSQL."""
        if not self._use_db:
            if session_id in self._in_memory:
                self._in_memory[session_id].update(updates)
            return self._in_memory.get(session_id, {})

        from app.db import session_store
        return await session_store.update_session(self._pool, session_id, updates)

    async def delete(self, session_id: str) -> bool:
        """Delete a session from PostgreSQL."""
        if not self._use_db:
            if session_id in self._in_memory:
                del self._in_memory[session_id]
            return True

        from app.db import session_store
        return await session_store.delete_session(self._pool, session_id)


class PostgresAuditStore:
    """PostgreSQL-backed audit store adapter.

    Wraps the db/audit_store functions for audit persistence.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._in_memory: list[dict[str, Any]] = []
        self._use_db = True

    async def record_event(self, event_data: dict[str, Any]) -> None:
        """Record an audit event in PostgreSQL."""
        if not self._use_db:
            self._in_memory.append(event_data)
            return

        from app.db import audit_store
        await audit_store.record_event(self._pool, event_data.get("session_id", ""), event_data)

    async def get_events(self, session_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        """Get events for a session from PostgreSQL."""
        if not self._use_db:
            return [e for e in self._in_memory if e.get("session_id") == session_id and e.get("seq", 0) > since_seq]

        from app.db import audit_store
        return await audit_store.get_events(self._pool, session_id, since_seq)

    async def get_decisions(self, session_id: str) -> list[dict[str, Any]]:
        """Get decision events for a session."""
        if not self._use_db:
            return [e for e in self._in_memory if e.get("session_id") == session_id and e.get("event_type") == "decision"]

        from app.db import audit_store
        return await audit_store.get_decisions(self._pool, session_id)


class PostgresCheckpointStore:
    """PostgreSQL-backed checkpoint store adapter.

    Wraps the db/checkpoint_store functions for crash recovery.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._in_memory: dict[str, dict[str, Any]] = {}
        self._use_db = True

    async def write_checkpoint(
        self,
        session_id: str,
        node_id: str,
        highest_seq: int,
        state_json: dict[str, Any] | None = None,
    ) -> None:
        """Write a checkpoint to PostgreSQL."""
        if not self._use_db:
            self._in_memory[session_id] = {
                "node_id": node_id,
                "highest_seq": highest_seq,
                "state": state_json,
            }
            return

        from app.db import checkpoint_store
        await checkpoint_store.write_checkpoint(
            self._pool, session_id, node_id, highest_seq, state_json
        )

    async def get_latest_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        """Get the latest checkpoint for a session."""
        if not self._use_db:
            return self._in_memory.get(session_id)

        from app.db import checkpoint_store
        return await checkpoint_store.get_latest_checkpoint(self._pool, session_id)

    async def list_non_terminal_sessions(self) -> list[dict[str, Any]]:
        """List sessions that are not in terminal state."""
        if not self._use_db:
            return []

        from app.db import checkpoint_store
        return await checkpoint_store.list_non_terminal_sessions(self._pool)


class PostgresLearningStore:
    """PostgreSQL-backed learning store adapter.

    Wraps the db/learning_store functions for outcome recording.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._in_memory: list[dict[str, Any]] = []
        self._use_db = True

    async def record_outcome(
        self,
        session_id: str,
        task_id: str,
        outcome_status: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record a learning outcome in PostgreSQL."""
        if not self._use_db:
            self._in_memory.append({
                "session_id": session_id,
                "task_id": task_id,
                "outcome_status": outcome_status,
                "data": data,
            })
            return

        from app.db import learning_store
        await learning_store.record_outcome(
            self._pool, session_id, task_id, outcome_status, data
        )

    async def get_outcomes(self, session_id: str) -> list[dict[str, Any]]:
        """Get learning outcomes for a session."""
        if not self._use_db:
            return [o for o in self._in_memory if o.get("session_id") == session_id]

        from app.db import learning_store
        return await learning_store.get_outcomes(self._pool, session_id)


async def is_persistence_available() -> bool:
    """Check if PostgreSQL persistence is available."""
    global _pool
    if _pool is not None:
        return True
    try:
        _pool = await get_pool()
        return _pool is not None
    except Exception:
        return False


def create_persistence_stores(pool: Any) -> tuple[PostgresSessionStore, PostgresAuditStore, PostgresCheckpointStore, PostgresLearningStore]:
    """Create all PostgreSQL-backed store instances.

    Args:
        pool: The asyncpg connection pool.

    Returns:
        Tuple of (session_store, audit_store, checkpoint_store, learning_store).
    """
    return (
        PostgresSessionStore(pool),
        PostgresAuditStore(pool),
        PostgresCheckpointStore(pool),
        PostgresLearningStore(pool),
    )
