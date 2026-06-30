"""Async CRUD operations for sessions using asyncpg."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg


async def create_session(pool: asyncpg.Pool, session_data: dict[str, Any]) -> dict[str, Any]:
    """Create a new session and return it as a dict.

    session_data should contain: repo_url, goal, build_mode.
    Optional: status, context_json.
    """
    session_id = session_data.get("id") or str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO sessions (id, repo_url, goal, build_mode, status, created_at, updated_at, context_json)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
            session_data["repo_url"],
            session_data["goal"],
            session_data["build_mode"],
            session_data.get("status", "pending"),
            now,
            now,
            session_data.get("context_json"),
        )

    return dict(row) if row else {}


async def get_session(pool: asyncpg.Pool, session_id: str) -> dict[str, Any] | None:
    """Get a session by ID. Returns None if not found."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM sessions WHERE id = $1",
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
        )

    return dict(row) if row else None


async def list_sessions(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """List all sessions ordered by creation time (newest first)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        )

    return [dict(row) for row in rows]


async def update_session(
    pool: asyncpg.Pool, session_id: str, updates: dict[str, Any]
) -> dict[str, Any]:
    """Update a session with the given fields. Returns updated session dict.

    Allowed update fields: repo_url, goal, build_mode, status, context_json.
    """
    allowed_fields = {"repo_url", "goal", "build_mode", "status", "context_json"}
    filtered = {k: v for k, v in updates.items() if k in allowed_fields}

    if not filtered:
        # Nothing to update, return current state
        result = await get_session(pool, session_id)
        if result is None:
            raise ValueError(f"Session {session_id} not found")
        return result

    # Build SET clause with parameterized placeholders
    set_parts = []
    params: list[Any] = []
    idx = 1

    for field_name, value in filtered.items():
        set_parts.append(f"{field_name} = ${idx}")
        params.append(value)
        idx += 1

    # Always update updated_at
    set_parts.append(f"updated_at = ${idx}")
    params.append(datetime.now(timezone.utc))
    idx += 1

    # Session ID is last param
    params.append(uuid.UUID(session_id) if isinstance(session_id, str) else session_id)

    query = f"UPDATE sessions SET {', '.join(set_parts)} WHERE id = ${idx} RETURNING *"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)

    if row is None:
        raise ValueError(f"Session {session_id} not found")

    return dict(row)


async def delete_session(pool: asyncpg.Pool, session_id: str) -> bool:
    """Delete a session by ID. Returns True if deleted, False if not found."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM sessions WHERE id = $1",
            uuid.UUID(session_id) if isinstance(session_id, str) else session_id,
        )

    # asyncpg returns e.g. "DELETE 1" or "DELETE 0"
    return result == "DELETE 1"
