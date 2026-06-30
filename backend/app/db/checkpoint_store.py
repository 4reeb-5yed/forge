"""Async checkpoint operations using asyncpg."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg


async def write_checkpoint(
    pool: asyncpg.Pool,
    session_id: str,
    node_id: str,
    highest_seq: int,
    state_json: dict[str, Any] | None = None,
) -> None:
    """Write a checkpoint for a session at the given node and sequence."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkpoints (session_id, node_id, highest_seq, state_json)
            VALUES ($1, $2, $3, $4)
            """,
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
            node_id,
            highest_seq,
            state_json,
        )


async def get_latest_checkpoint(
    pool: asyncpg.Pool, session_id: str
) -> dict[str, Any] | None:
    """Get the most recent checkpoint for a session (by highest_seq desc)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM checkpoints
            WHERE session_id = $1
            ORDER BY highest_seq DESC
            LIMIT 1
            """,
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
        )

    return dict(row) if row else None


async def list_non_terminal_sessions(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """List sessions that are not in a terminal status (completed, failed, cancelled).

    Joins checkpoints to return the latest checkpoint per non-terminal session.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id AS session_id, s.status, s.goal, s.repo_url,
                   c.node_id, c.highest_seq, c.state_json, c.created_at AS checkpoint_at
            FROM sessions s
            LEFT JOIN LATERAL (
                SELECT * FROM checkpoints cp
                WHERE cp.session_id = s.id
                ORDER BY cp.highest_seq DESC
                LIMIT 1
            ) c ON true
            WHERE s.status NOT IN ('completed', 'failed', 'cancelled')
            ORDER BY s.created_at DESC
            """
        )

    return [dict(row) for row in rows]
