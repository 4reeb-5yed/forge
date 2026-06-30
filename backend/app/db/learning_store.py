"""Async learning outcome operations using asyncpg."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg


async def record_outcome(
    pool: asyncpg.Pool,
    session_id: str,
    task_id: str,
    outcome_status: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Record a learning outcome for a task in a session."""
    outcome_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO learning_outcomes (id, session_id, task_id, outcome_status, data_json)
            VALUES ($1, $2, $3, $4, $5)
            """,
            uuid.UUID(outcome_id),
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
            task_id,
            outcome_status,
            data,
        )


async def get_outcomes(pool: asyncpg.Pool, session_id: str) -> list[dict[str, Any]]:
    """Get all learning outcomes for a session, ordered by creation time."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM learning_outcomes
            WHERE session_id = $1
            ORDER BY created_at ASC
            """,
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
        )

    return [dict(row) for row in rows]
