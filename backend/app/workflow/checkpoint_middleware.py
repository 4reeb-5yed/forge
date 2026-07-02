"""Checkpoint Middleware - Automatic workflow state persistence.

Wraps workflow node execution to automatically save checkpoints after each node,
enabling crash recovery and resume functionality.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# Default checkpoint interval (every N seconds, 0 = only at nodes)
DEFAULT_CHECKPOINT_INTERVAL_SECONDS = int(os.environ.get("FORGE_CHECKPOINT_INTERVAL", "60"))


class CheckpointMiddleware:
    """Middleware that wraps workflow execution for automatic checkpointing.

    Automatically saves checkpoints:
    - After each node execution
    - At configurable intervals during long operations
    - On key lifecycle events

    This enables crash recovery by resuming from the last checkpoint.
    """

    def __init__(
        self,
        checkpoint_store: Any | None = None,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL_SECONDS,
    ) -> None:
        """Initialize the CheckpointMiddleware.

        Args:
            checkpoint_store: The checkpoint store implementation.
            checkpoint_interval: Seconds between periodic checkpoints (0 = disabled).
        """
        self._store = checkpoint_store
        self._interval = checkpoint_interval
        self._last_checkpoint: dict[str, float] = {}  # session_id -> last checkpoint time
        self._lock = asyncio.Lock()

    def wrap_node(
        self,
        node_name: str,
        node_fn: Callable[..., Awaitable[dict[str, Any]]],
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        """Wrap a node function to add automatic checkpointing.

        Args:
            node_name: Name of the node for logging.
            node_fn: The original node function.

        Returns:
            Wrapped function that checkpoints after execution.
        """
        async def wrapped(state: dict[str, Any]) -> dict[str, Any]:
            session_id = state.get("session_id", "unknown")
            
            # Execute the original node
            try:
                result = await node_fn(state)
            except Exception as exc:
                logger.error("Node %s failed: %s", node_name, exc)
                raise

            # Add current node to result
            if "node_path" in result:
                result["node_path"] = list(result["node_path"]) + [node_name]
            else:
                result["node_path"] = [node_name]

            # Checkpoint after node execution
            await self.checkpoint(
                session_id=session_id,
                node_id=node_name,
                state={**state, **result},
            )

            return result

        # Preserve function metadata
        wrapped.__name__ = f"checkpointed_{node_name}"
        wrapped.__doc__ = node_fn.__doc__

        return wrapped

    async def checkpoint(
        self,
        session_id: str,
        node_id: str,
        state: dict[str, Any],
    ) -> None:
        """Save a checkpoint of the current workflow state.

        Args:
            session_id: The session ID.
            node_id: The current node ID.
            state: The current workflow state.
        """
        if self._store is None:
            return

        try:
            # Calculate the highest sequence number seen
            events = state.get("events", [])
            if hasattr(events, "__len__"):
                highest_seq = max((e.get("seq", 0) for e in events), default=0)
            else:
                highest_seq = 0

            # Create redacted state for storage (exclude sensitive data)
            redacted_state = self._redact_state(state)

            # Serialize state to JSON string for asyncpg compatibility
            state_json = json.dumps(redacted_state)

            # Write checkpoint
            await self._store.write_checkpoint(
                session_id=session_id,
                node_id=node_id,
                highest_seq=highest_seq,
                state_json=state_json,
            )

            # Update last checkpoint time
            import time
            self._last_checkpoint[session_id] = time.time()

            logger.debug(
                "Checkpoint saved: session=%s node=%s seq=%d",
                session_id, node_id, highest_seq
            )

        except Exception as exc:
            logger.warning("Checkpoint failed for session %s: %s", session_id, exc)

    async def should_checkpoint(self, session_id: str) -> bool:
        """Check if a periodic checkpoint should be performed.

        Args:
            session_id: The session ID.

        Returns:
            True if checkpoint should be performed.
        """
        if self._interval <= 0:
            return False

        import time
        last = self._last_checkpoint.get(session_id, 0)
        return (time.time() - last) >= self._interval

    def _redact_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Create a redacted copy of state for storage.

        Removes sensitive data like tokens and secrets.

        Args:
            state: The original state.

        Returns:
            Redacted state copy.
        """
        import copy

        # Deep copy to avoid modifying original
        redacted = copy.deepcopy(state)

        # Remove or redact sensitive fields
        sensitive_fields = {
            "token", "vcs_token", "github_token", "api_key",
            "secret", "password", "credentials",
        }

        def redact_dict(d: dict) -> dict:
            result = {}
            for key, value in d.items():
                if key.lower() in sensitive_fields:
                    result[key] = "[REDACTED]"
                elif isinstance(value, dict):
                    result[key] = redact_dict(value)
                elif isinstance(value, list):
                    result[key] = [
                        redact_dict(item) if isinstance(item, dict) else item
                        for item in value
                    ]
                else:
                    result[key] = value
            return result

        return redact_dict(redacted)

    async def recover(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Attempt to recover workflow state from checkpoint.

        Args:
            session_id: The session ID to recover.

        Returns:
            Recovered state if checkpoint found, None otherwise.
        """
        if self._store is None:
            return None

        try:
            checkpoint = await self._store.get_latest_checkpoint(session_id)
            if checkpoint is None:
                logger.info("No checkpoint found for session %s", session_id)
                return None

            logger.info(
                "Recovering session %s from node %s (seq=%d)",
                session_id,
                checkpoint.get("node_id"),
                checkpoint.get("highest_seq"),
            )

            # Get the stored state and make a copy to avoid mutating the stored data
            stored_state = checkpoint.get("state", {})
            if not isinstance(stored_state, dict):
                logger.warning(
                    "Checkpoint state for session %s is not a dict (got %s) — treating as empty",
                    session_id, type(stored_state).__name__
                )
                stored_state = {}

            # Build recovered state with metadata
            state = dict(stored_state)  # shallow copy of the state dict
            state["recovered_from_checkpoint"] = True
            state["checkpoint_node_id"] = checkpoint.get("node_id")
            state["checkpoint_seq"] = checkpoint.get("highest_seq")

            return state

        except Exception as exc:
            logger.error("Recovery failed for session %s: %s", session_id, exc)
            return None

    async def list_recoverable_sessions(self) -> list[dict[str, Any]]:
        """List all sessions that can be recovered.

        Returns:
            List of recoverable session info.
        """
        if self._store is None:
            return []

        try:
            return await self._store.list_non_terminal_sessions()
        except Exception as exc:
            logger.error("Failed to list recoverable sessions: %s", exc)
            return []


# Global middleware instance
_checkpoint_middleware: CheckpointMiddleware | None = None


def get_checkpoint_middleware() -> CheckpointMiddleware | None:
    """Get the global checkpoint middleware instance."""
    return _checkpoint_middleware


def set_checkpoint_middleware(middleware: CheckpointMiddleware) -> None:
    """Set the global checkpoint middleware instance."""
    global _checkpoint_middleware
    _checkpoint_middleware = middleware


def create_checkpoint_middleware(
    checkpoint_store: Any | None = None,
) -> CheckpointMiddleware:
    """Create and configure a checkpoint middleware.

    Args:
        checkpoint_store: The checkpoint store to use.

    Returns:
        Configured CheckpointMiddleware instance.
    """
    global _checkpoint_middleware
    _checkpoint_middleware = CheckpointMiddleware(checkpoint_store=checkpoint_store)
    return _checkpoint_middleware
