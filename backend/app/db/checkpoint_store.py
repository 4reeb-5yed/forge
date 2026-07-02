"""Async checkpoint operations using asyncpg."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


def _coerce_session_uuid(session_id: str) -> uuid.UUID | None:
    """Coerce a session_id into a UUID for the `checkpoints.session_id` FK column.

    `checkpoints.session_id` is a foreign key into `sessions.id` (a UUID column).
    Callers of this module — notably `/workflow/invoke` — accept an arbitrary,
    client-supplied `session_id` string (e.g. "demo-1", "e2e-sandbox-final") with
    no corresponding `sessions` row ever created. Passing such a string straight
    to `uuid.UUID(...)` raises `ValueError: badly formed hexadecimal UUID string`,
    and even a real UUID would still fail with a foreign-key violation if no
    matching `sessions` row exists.

    Returns the parsed UUID if `session_id` is a valid UUID string, or `None` if
    it isn't. Callers should treat `None` as "skip the checkpoint write/read for
    this session" rather than raising, since this is expected for any session
    that predates the PostgreSQL-backed session store being wired into the actual
    request path.
    """
    try:
        return uuid.UUID(session_id) if isinstance(session_id, str) else session_id
    except (ValueError, AttributeError, TypeError):
        logger.warning(
            "session_id %r is not a valid UUID — checkpoint store requires a "
            "sessions.id foreign key match. Skipping checkpoint operation for "
            "this session instead of raising.",
            session_id,
        )
        return None


async def write_checkpoint(
    pool: asyncpg.Pool,
    session_id: str,
    node_id: str,
    highest_seq: int,
    state_json: str | None = None,
) -> None:
    """Write a checkpoint for a session at the given node and sequence.

    No-ops (logs a warning, does not raise) if `session_id` is not a valid UUID —
    see `_coerce_session_uuid`.
    """
    session_uuid = _coerce_session_uuid(session_id)
    if session_uuid is None:
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO checkpoints (session_id, node_id, highest_seq, state_json)
            VALUES ($1, $2, $3, $4)
            """,
            session_uuid,
            node_id,
            highest_seq,
            state_json,
        )


async def get_latest_checkpoint(
    pool: asyncpg.Pool, session_id: str
) -> dict[str, Any] | None:
    """Get the most recent checkpoint for a session (by highest_seq desc).

    Returns `None` if `session_id` is not a valid UUID — see `_coerce_session_uuid`.

    The returned dict contains:
    - node_id: str
    - highest_seq: int
    - state: dict (parsed from state_json column)
    - (other columns from the row)
    """
    session_uuid = _coerce_session_uuid(session_id)
    if session_uuid is None:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM checkpoints
            WHERE session_id = $1
            ORDER BY highest_seq DESC
            LIMIT 1
            """,
            session_uuid,
        )

    if not row:
        return None

    # Convert row to dict
    result = dict(row)

    # Parse state_json into a dict and expose as 'state' for consistency
    state_json = result.get("state_json")
    if state_json is not None:
        if isinstance(state_json, str):
            try:
                result["state"] = json.loads(state_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to parse state_json for session %s — returning empty state",
                    session_id,
                )
                result["state"] = {}
        else:
            # Already parsed (shouldn't happen with asyncpg unless codec registered)
            result["state"] = state_json
    else:
        result["state"] = {}

    return result


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
