"""Async audit log operations using asyncpg."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg


async def record_event(
    pool: asyncpg.Pool, session_id: str, event_data: dict[str, Any]
) -> None:
    """Insert an audit event. Enforces (session_id, seq) uniqueness.

    event_data should contain: seq, event_type.
    Optional: source, payload, causation_id, correlation_id, event_id.
    """
    event_id_val = event_data.get("event_id") or str(uuid.uuid4())
    row_id = event_data.get("id") or str(uuid.uuid4())

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log
                (id, session_id, seq, event_type, source, payload,
                 causation_id, correlation_id, event_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uuid.UUID(row_id) if isinstance(row_id, str) else row_id,
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
            event_data["seq"],
            event_data["event_type"],
            event_data.get("source"),
            event_data.get("payload"),
            event_data.get("causation_id"),
            event_data.get("correlation_id"),
            event_id_val,
        )


async def get_events(
    pool: asyncpg.Pool, session_id: str, since_seq: int = 0
) -> list[dict[str, Any]]:
    """Get audit events for a session, ordered by seq, starting after since_seq."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM audit_log
            WHERE session_id = $1 AND seq > $2
            ORDER BY seq ASC
            """,
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
            since_seq,
        )

    return [dict(row) for row in rows]


async def get_decisions(pool: asyncpg.Pool, session_id: str) -> list[dict[str, Any]]:
    """Get decision events for a session (event_type = 'decision')."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM audit_log
            WHERE session_id = $1 AND event_type = 'decision'
            ORDER BY seq ASC
            """,
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
        )

    return [dict(row) for row in rows]
