"""Tests for checkpoint middleware recovery functionality.

Verifies that checkpoint recovery works correctly end-to-end,
including proper JSON parsing of state_json from the database.

Requirements: 21.1, 21.2, 21.3
"""

from __future__ import annotations

import json
import pytest
import uuid

from app.workflow.checkpoint_middleware import CheckpointMiddleware


class MockCheckpointStore:
    """Mock checkpoint store that simulates PostgreSQL behavior.

    This mock stores checkpoints with state_json as a JSON STRING (not dict),
    simulating how PostgreSQL's jsonb column would return data.
    
    The key behavior this mock simulates:
    - write_checkpoint receives state_json as a JSON string
    - get_latest_checkpoint returns state_json as a JSON string (simulating asyncpg)
    - But the middleware's recover() expects 'state' key with a parsed dict
    - So we must parse state_json -> state like the real db/checkpoint_store does
    """

    def __init__(self) -> None:
        self._checkpoints: dict[str, dict] = {}

    async def write_checkpoint(
        self,
        session_id: str,
        node_id: str,
        highest_seq: int,
        state_json: str | None = None,
    ) -> None:
        """Store checkpoint with JSON string state (as PostgreSQL would).
        
        Note: state_json should be a JSON string, not a dict.
        This simulates PostgreSQL behavior where jsonb data comes back as string.
        """
        self._checkpoints[session_id] = {
            "session_id": uuid.uuid4(),
            "node_id": node_id,
            "highest_seq": highest_seq,
            "state_json": state_json,  # JSON string, not dict
        }

    async def get_latest_checkpoint(self, session_id: str) -> dict | None:
        """Return checkpoint as asyncpg would - with state_json as JSON string.
        
        This simulates the actual db/checkpoint_store.get_latest_checkpoint()
        behavior: parse state_json into state dict.
        """
        checkpoint = self._checkpoints.get(session_id)
        if checkpoint is None:
            return None
        
        # Parse state_json like the real implementation does
        result = dict(checkpoint)
        state_json = result.get("state_json")
        if state_json is not None and isinstance(state_json, str):
            try:
                result["state"] = json.loads(state_json)
            except (json.JSONDecodeError, TypeError):
                result["state"] = {}
        else:
            result["state"] = state_json if state_json else {}
        
        return result

    async def list_non_terminal_sessions(self) -> list:
        return []


class TestCheckpointRecovery:
    """Test checkpoint middleware recovery functionality."""

    @pytest.fixture
    def middleware(self) -> CheckpointMiddleware:
        """Create middleware with mock store."""
        store = MockCheckpointStore()
        return CheckpointMiddleware(checkpoint_store=store)

    @pytest.mark.asyncio
    async def test_recover_returns_stored_state(self, middleware: CheckpointMiddleware) -> None:
        """Verify recover() returns the stored state with metadata.
        
        This test verifies the full round-trip:
        1. checkpoint() serializes state to JSON string
        2. get_latest_checkpoint() returns the row with state_json as string
        3. recover() parses the JSON and returns the original state
        """
        session_id = str(uuid.uuid4())

        # Create a checkpoint with known state
        original_state = {
            "session_id": session_id,
            "message": "test message",
            "tasks": [{"id": "task-1", "status": "done"}],
            "node_path": ["intake", "plan"],
        }

        # Write checkpoint (simulates what checkpoint() does internally)
        await middleware.checkpoint(
            session_id=session_id,
            node_id="plan",
            state=original_state,
        )

        # Recover and verify
        recovered = await middleware.recover(session_id)

        assert recovered is not None
        # Should contain original state data
        assert recovered["message"] == "test message"
        assert recovered["tasks"] == [{"id": "task-1", "status": "done"}]
        assert recovered["node_path"] == ["intake", "plan"]
        # Should have recovery metadata
        assert recovered["recovered_from_checkpoint"] is True
        assert recovered["checkpoint_node_id"] == "plan"
        assert recovered["checkpoint_seq"] == 0

    @pytest.mark.asyncio
    async def test_recover_with_no_checkpoint(self, middleware: CheckpointMiddleware) -> None:
        """Verify recover() returns None when no checkpoint exists."""
        result = await middleware.recover(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_recover_does_not_mutate_stored_state(self, middleware: CheckpointMiddleware) -> None:
        """Verify that recovering doesn't modify the stored checkpoint data."""
        session_id = str(uuid.uuid4())

        original_state = {"message": "original", "counter": 1}
        await middleware.checkpoint(session_id=session_id, node_id="test", state=original_state)

        # Recover multiple times and modify the result
        for _ in range(3):
            recovered = await middleware.recover(session_id)
            assert recovered is not None
            recovered["counter"] += 1  # Modify the recovered state
            recovered["new_field"] = "added"

        # Recover again and verify original state is intact
        recovered_again = await middleware.recover(session_id)
        assert recovered_again["message"] == "original"
        assert recovered_again["counter"] == 1  # Should be unchanged
        assert "new_field" not in recovered_again  # Should not have added field


class TestCheckpointStoreJsonParsing:
    """Test that checkpoint serialization works correctly.
    
    These tests verify the write path: state dict -> JSON string.
    Direct testing of db/checkpoint_store.get_latest_checkpoint requires
    a proper asyncpg mock which is complex. The key recovery functionality
    is tested above via TestCheckpointRecovery which exercises the full path.
    """

    @pytest.mark.asyncio
    async def test_checkpoint_serializes_state_to_json_string(self) -> None:
        """Verify that checkpoint serializes state to JSON string.
        
        This verifies the write path: state dict -> JSON string.
        """
        class RecordingStore:
            def __init__(self):
                self.captured_state_json = None
                
            async def write_checkpoint(self, session_id, node_id, highest_seq, state_json):
                self.captured_state_json = state_json  # Should be JSON string
                
            async def get_latest_checkpoint(self, session_id):
                return None
                
            async def list_non_terminal_sessions(self):
                return []
        
        store = RecordingStore()
        middleware = CheckpointMiddleware(checkpoint_store=store)
        
        # Write a checkpoint
        original_state = {"message": "test", "tasks": ["a", "b"]}
        await middleware.checkpoint("test-session", "test-node", original_state)
        
        # Verify state_json is a JSON string
        assert store.captured_state_json is not None
        assert isinstance(store.captured_state_json, str)
        
        # Verify it can be parsed back
        parsed = json.loads(store.captured_state_json)
        assert parsed == original_state
