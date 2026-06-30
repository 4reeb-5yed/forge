"""Unit tests for the Crash Recovery module.

Tests cover:
- Checkpoint persistence with redacted state after each node (Req 21.1)
- Halt on checkpoint write failure with error emission (Req 21.2)
- Resume from last completed node on restart (Req 21.3)
- Workspace destruction and task re-queuing for uncommitted tasks (Req 21.4)
- Event replay on client reconnect in seq order (Req 21.5)
- State snapshot + retained events for stale clients (Req 21.6)

Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.runtime.events.models import Event, EventType
from app.runtime.recovery import (
    Checkpoint,
    CheckpointWriteError,
    ClientReconnectResult,
    CrashRecovery,
    InMemoryCheckpointStore,
    ResumeResult,
    TERMINAL_NODES,
)


# --- Helpers ---


def _make_event(
    session_id: str = "session-1",
    seq: int = 1,
    event_type: EventType = EventType.TASK_START,
    source: str = "test",
    payload: dict | None = None,
    event_id: str | None = None,
) -> Event:
    """Helper to create a test Event."""
    return Event(
        schema_version=1,
        seq=seq,
        session_id=session_id,
        type=event_type,
        timestamp=datetime.now(timezone.utc),
        source=source,
        payload=payload or {"task_id": "t1"},
        event_id=event_id or str(uuid.uuid4()),
    )


def _make_state(
    session_id: str = "session-1",
    status: str = "running",
    tasks: list | None = None,
) -> dict:
    """Helper to create a ForgeState-like dict."""
    return {
        "session_id": session_id,
        "status": status,
        "tasks": tasks or [],
        "current_task_id": None,
    }


def _make_recovery(
    store: InMemoryCheckpointStore | None = None,
    event_bus: AsyncMock | None = None,
    event_replayer: AsyncMock | None = None,
    workspace_destroyer: AsyncMock | None = None,
    task_requeuer: AsyncMock | None = None,
    redact_state=None,
    retained_event_window: int = 1000,
) -> CrashRecovery:
    """Helper to create a CrashRecovery instance with defaults."""
    return CrashRecovery(
        checkpoint_store=store or InMemoryCheckpointStore(),
        event_bus=event_bus,
        event_replayer=event_replayer,
        workspace_destroyer=workspace_destroyer,
        task_requeuer=task_requeuer,
        redact_state=redact_state,
        retained_event_window=retained_event_window,
    )


# --- Test: Checkpoint dataclass ---


class TestCheckpoint:
    """Tests for the Checkpoint dataclass."""

    def test_checkpoint_creation(self) -> None:
        """Checkpoint captures session_id, node_id, highest_seq, and state."""
        cp = Checkpoint(
            session_id="s1",
            node_id="architect",
            highest_seq=5,
            redacted_state={"session_id": "s1", "status": "running"},
        )
        assert cp.session_id == "s1"
        assert cp.node_id == "architect"
        assert cp.highest_seq == 5
        assert cp.redacted_state == {"session_id": "s1", "status": "running"}

    def test_checkpoint_is_terminal_for_terminal_nodes(self) -> None:
        """Checkpoints at terminal nodes report is_terminal=True."""
        for node in TERMINAL_NODES:
            cp = Checkpoint(session_id="s1", node_id=node, highest_seq=1)
            assert cp.is_terminal is True

    def test_checkpoint_is_not_terminal_for_active_nodes(self) -> None:
        """Checkpoints at non-terminal nodes report is_terminal=False."""
        for node in ["architect", "plan", "execute", "verify", "commit"]:
            cp = Checkpoint(session_id="s1", node_id=node, highest_seq=1)
            assert cp.is_terminal is False


# --- Test: InMemoryCheckpointStore ---


class TestInMemoryCheckpointStore:
    """Tests for the in-memory checkpoint store."""

    async def test_write_and_get_latest_checkpoint(self) -> None:
        """Written checkpoint is retrievable as the latest."""
        store = InMemoryCheckpointStore()
        await store.write_checkpoint("s1", "architect", 3, {"status": "running"})

        latest = await store.get_latest_checkpoint("s1")
        assert latest is not None
        assert latest.session_id == "s1"
        assert latest.node_id == "architect"
        assert latest.highest_seq == 3

    async def test_get_latest_returns_most_recent(self) -> None:
        """When multiple checkpoints exist, get_latest returns the last one."""
        store = InMemoryCheckpointStore()
        await store.write_checkpoint("s1", "architect", 3, {"step": 1})
        await store.write_checkpoint("s1", "plan", 7, {"step": 2})
        await store.write_checkpoint("s1", "execute", 12, {"step": 3})

        latest = await store.get_latest_checkpoint("s1")
        assert latest is not None
        assert latest.node_id == "execute"
        assert latest.highest_seq == 12

    async def test_get_latest_returns_none_for_unknown_session(self) -> None:
        """Returns None when no checkpoints exist for the session."""
        store = InMemoryCheckpointStore()
        assert await store.get_latest_checkpoint("unknown") is None

    async def test_list_non_terminal_sessions(self) -> None:
        """Lists sessions whose latest checkpoint is non-terminal."""
        store = InMemoryCheckpointStore()
        # Session 1 is at a non-terminal node
        await store.write_checkpoint("s1", "execute", 5, {})
        # Session 2 is at a terminal node
        await store.write_checkpoint("s2", "finalize", 10, {})
        # Session 3 is at a non-terminal node
        await store.write_checkpoint("s3", "verify", 8, {})

        results = await store.list_non_terminal_sessions()
        session_ids = {r.session_id for r in results}
        assert session_ids == {"s1", "s3"}

    async def test_write_failure_raises_checkpoint_write_error(self) -> None:
        """Configured failure raises CheckpointWriteError."""
        store = InMemoryCheckpointStore()
        store.set_fail_next_write("disk full")

        with pytest.raises(CheckpointWriteError) as exc_info:
            await store.write_checkpoint("s1", "plan", 5, {})

        assert exc_info.value.session_id == "s1"
        assert exc_info.value.node_id == "plan"
        assert "disk full" in exc_info.value.reason

    async def test_clear_removes_all_checkpoints(self) -> None:
        """Clear removes all checkpoints from the store."""
        store = InMemoryCheckpointStore()
        await store.write_checkpoint("s1", "execute", 5, {})
        await store.write_checkpoint("s2", "plan", 3, {})

        store.clear()

        assert await store.get_latest_checkpoint("s1") is None
        assert await store.get_latest_checkpoint("s2") is None

    async def test_get_all_returns_ordered_checkpoints(self) -> None:
        """get_all returns all checkpoints for a session in order."""
        store = InMemoryCheckpointStore()
        await store.write_checkpoint("s1", "architect", 3, {})
        await store.write_checkpoint("s1", "plan", 7, {})

        all_cps = store.get_all("s1")
        assert len(all_cps) == 2
        assert all_cps[0].node_id == "architect"
        assert all_cps[1].node_id == "plan"


# --- Test: Checkpoint after each node (Req 21.1) ---


class TestCheckpointAfterNode:
    """Tests for checkpoint_after_node — Req 21.1."""

    async def test_checkpoint_persists_redacted_state(self) -> None:
        """Checkpoint writes redacted state to the store (Req 21.1)."""
        store = InMemoryCheckpointStore()
        secret_token = "ghp_secret123"

        def mock_redact(state, session_id=None):
            # Replace secret with placeholder
            result = dict(state)
            for k, v in result.items():
                if isinstance(v, str) and secret_token in v:
                    result[k] = "***REDACTED***"
            return result

        recovery = _make_recovery(store=store, redact_state=mock_redact)

        state = {"session_id": "s1", "vcs_token": secret_token, "status": "running"}
        await recovery.checkpoint_after_node("s1", "architect", state, 5)

        latest = await store.get_latest_checkpoint("s1")
        assert latest is not None
        assert latest.redacted_state["vcs_token"] == "***REDACTED***"
        assert latest.node_id == "architect"
        assert latest.highest_seq == 5

    async def test_checkpoint_stores_node_id_and_seq(self) -> None:
        """Checkpoint stores the completed node ID and highest seq (Req 21.1)."""
        store = InMemoryCheckpointStore()
        recovery = _make_recovery(store=store)

        await recovery.checkpoint_after_node("s1", "plan", {"status": "ok"}, 10)

        latest = await store.get_latest_checkpoint("s1")
        assert latest is not None
        assert latest.node_id == "plan"
        assert latest.highest_seq == 10

    async def test_multiple_checkpoints_track_progress(self) -> None:
        """Each node completion creates a new checkpoint."""
        store = InMemoryCheckpointStore()
        recovery = _make_recovery(store=store)

        await recovery.checkpoint_after_node("s1", "architect", {}, 3)
        await recovery.checkpoint_after_node("s1", "plan", {}, 7)
        await recovery.checkpoint_after_node("s1", "execute", {}, 12)

        all_cps = store.get_all("s1")
        assert len(all_cps) == 3
        assert [cp.node_id for cp in all_cps] == ["architect", "plan", "execute"]


# --- Test: Halt on checkpoint write failure (Req 21.2) ---


class TestCheckpointWriteFailure:
    """Tests for halt on checkpoint write failure — Req 21.2."""

    async def test_halts_on_write_failure(self) -> None:
        """Raises CheckpointWriteError on write failure, halting the workflow."""
        store = InMemoryCheckpointStore()
        store.set_fail_next_write("I/O error")
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock(return_value=_make_event())
        recovery = _make_recovery(store=store, event_bus=event_bus)

        with pytest.raises(CheckpointWriteError) as exc_info:
            await recovery.checkpoint_after_node("s1", "plan", {}, 5)

        assert exc_info.value.session_id == "s1"
        assert exc_info.value.node_id == "plan"

    async def test_emits_error_event_on_failure(self) -> None:
        """Emits an error event when checkpoint write fails (Req 21.2)."""
        store = InMemoryCheckpointStore()
        store.set_fail_next_write("connection lost")
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock(return_value=_make_event())
        recovery = _make_recovery(store=store, event_bus=event_bus)

        with pytest.raises(CheckpointWriteError):
            await recovery.checkpoint_after_node("s1", "execute", {}, 8)

        # Verify error event was published
        event_bus.publish.assert_called_once()
        published_event = event_bus.publish.call_args[0][0]
        assert published_event.type == EventType.ERROR
        assert published_event.session_id == "s1"
        assert "checkpoint" in published_event.payload["where"]

    async def test_does_not_advance_on_failure(self) -> None:
        """No checkpoint is stored when write fails (workflow does not advance)."""
        store = InMemoryCheckpointStore()
        store.set_fail_next_write("timeout")
        recovery = _make_recovery(store=store)

        with pytest.raises(CheckpointWriteError):
            await recovery.checkpoint_after_node("s1", "execute", {}, 5)

        # No checkpoint should exist
        assert await store.get_latest_checkpoint("s1") is None


# --- Test: Resume on restart (Req 21.3) ---


class TestResumeOnRestart:
    """Tests for session resume on restart — Req 21.3."""

    async def test_finds_resumable_sessions(self) -> None:
        """Identifies sessions with non-terminal checkpoints on restart."""
        store = InMemoryCheckpointStore()
        await store.write_checkpoint("s1", "execute", 10, {"status": "running"})
        await store.write_checkpoint("s2", "finalize", 20, {"status": "done"})
        await store.write_checkpoint("s3", "verify", 15, {"status": "verifying"})

        recovery = _make_recovery(store=store)
        results = await recovery.resume_all()

        resumed_ids = {r.session_id for r in results}
        assert resumed_ids == {"s1", "s3"}

    async def test_resume_returns_last_checkpoint_state(self) -> None:
        """Resume provides the last checkpoint's state and node."""
        store = InMemoryCheckpointStore()
        state = {"session_id": "s1", "status": "running", "tasks": []}
        await store.write_checkpoint("s1", "execute", 10, state)

        recovery = _make_recovery(store=store)
        results = await recovery.resume_all()

        assert len(results) == 1
        result = results[0]
        assert result.session_id == "s1"
        assert result.node_id == "execute"
        assert result.highest_seq == 10
        assert result.state == state
        assert result.success is True

    async def test_resume_no_sessions_returns_empty(self) -> None:
        """When no sessions need resuming, returns empty list."""
        store = InMemoryCheckpointStore()
        await store.write_checkpoint("s1", "finalize", 20, {})

        recovery = _make_recovery(store=store)
        results = await recovery.resume_all()
        assert results == []


# --- Test: Workspace destruction and task re-queuing (Req 21.4) ---


class TestUncommittedTaskRecovery:
    """Tests for uncommitted task handling — Req 21.4."""

    async def test_destroys_workspace_for_uncommitted_running_task(self) -> None:
        """Destroys workspace for a task that was running but not committed."""
        store = InMemoryCheckpointStore()
        state = {
            "session_id": "s1",
            "status": "running",
            "tasks": [{"id": "t1", "status": "running"}],
        }
        await store.write_checkpoint("s1", "execute", 10, state)

        workspace_destroyer = AsyncMock()
        workspace_destroyer.destroy = AsyncMock()
        task_requeuer = AsyncMock()
        task_requeuer.requeue_task = AsyncMock(return_value=True)
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock(return_value=_make_event())

        recovery = _make_recovery(
            store=store,
            workspace_destroyer=workspace_destroyer,
            task_requeuer=task_requeuer,
            event_bus=event_bus,
        )
        # Register a workspace for the task
        recovery.register_task_workspace("t1", "ws-1")

        results = await recovery.resume_all()

        assert len(results) == 1
        result = results[0]
        assert "ws-1" in result.workspaces_destroyed
        workspace_destroyer.destroy.assert_called_once_with("ws-1", "crash_recovery")

    async def test_requeues_uncommitted_task_within_budget(self) -> None:
        """Re-queues uncommitted task if within retry budget."""
        store = InMemoryCheckpointStore()
        state = {
            "session_id": "s1",
            "status": "running",
            "tasks": [{"id": "t1", "status": "verifying"}],
        }
        await store.write_checkpoint("s1", "execute", 10, state)

        task_requeuer = AsyncMock()
        task_requeuer.requeue_task = AsyncMock(return_value=True)

        recovery = _make_recovery(store=store, task_requeuer=task_requeuer)
        results = await recovery.resume_all()

        assert len(results) == 1
        assert "t1" in results[0].tasks_requeued
        task_requeuer.requeue_task.assert_called_once_with("s1", "t1")

    async def test_does_not_requeue_committed_task(self) -> None:
        """Tasks that have commit.done recorded are not re-queued."""
        store = InMemoryCheckpointStore()
        state = {
            "session_id": "s1",
            "status": "running",
            "tasks": [{"id": "t1", "status": "running"}],
        }
        await store.write_checkpoint("s1", "execute", 10, state)

        task_requeuer = AsyncMock()
        task_requeuer.requeue_task = AsyncMock(return_value=True)

        recovery = _make_recovery(store=store, task_requeuer=task_requeuer)
        # Mark task as committed before recovery
        recovery.record_commit("s1", "t1")

        results = await recovery.resume_all()

        assert results[0].tasks_requeued == []
        task_requeuer.requeue_task.assert_not_called()

    async def test_does_not_requeue_pending_tasks(self) -> None:
        """Tasks that were pending (not started) are not re-queued."""
        store = InMemoryCheckpointStore()
        state = {
            "session_id": "s1",
            "status": "running",
            "tasks": [{"id": "t1", "status": "pending"}],
        }
        await store.write_checkpoint("s1", "execute", 10, state)

        task_requeuer = AsyncMock()
        task_requeuer.requeue_task = AsyncMock(return_value=True)

        recovery = _make_recovery(store=store, task_requeuer=task_requeuer)
        results = await recovery.resume_all()

        assert results[0].tasks_requeued == []
        task_requeuer.requeue_task.assert_not_called()

    async def test_emits_workspace_destroyed_event_on_recovery(self) -> None:
        """Emits workspace.destroyed event when destroying workspace (Req 21.4)."""
        store = InMemoryCheckpointStore()
        state = {
            "session_id": "s1",
            "status": "running",
            "tasks": [{"id": "t1", "status": "running"}],
        }
        await store.write_checkpoint("s1", "execute", 10, state)

        workspace_destroyer = AsyncMock()
        workspace_destroyer.destroy = AsyncMock()
        task_requeuer = AsyncMock()
        task_requeuer.requeue_task = AsyncMock(return_value=True)
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock(return_value=_make_event())

        recovery = _make_recovery(
            store=store,
            workspace_destroyer=workspace_destroyer,
            task_requeuer=task_requeuer,
            event_bus=event_bus,
        )
        recovery.register_task_workspace("t1", "ws-crash")

        await recovery.resume_all()

        # Verify workspace.destroyed event was published
        published_events = [call[0][0] for call in event_bus.publish.call_args_list]
        ws_destroyed_events = [
            e for e in published_events
            if e.type == EventType.WORKSPACE_DESTROYED
        ]
        assert len(ws_destroyed_events) == 1
        assert ws_destroyed_events[0].payload["workspace_id"] == "ws-crash"
        assert ws_destroyed_events[0].payload["task_id"] == "t1"
        assert ws_destroyed_events[0].payload["reason"] == "crash_recovery"


# --- Test: Event replay on client reconnect (Req 21.5) ---


class TestClientReconnectReplay:
    """Tests for event replay on client reconnect — Req 21.5."""

    async def test_replays_events_since_last_seq(self) -> None:
        """Replays events strictly after client's last received seq."""
        event_replayer = AsyncMock()
        events = [
            _make_event(session_id="s1", seq=4),
            _make_event(session_id="s1", seq=5),
            _make_event(session_id="s1", seq=6),
        ]
        event_replayer.replay = AsyncMock(return_value=events)

        recovery = _make_recovery(event_replayer=event_replayer)
        result = await recovery.handle_client_reconnect("s1", last_received_seq=3)

        assert len(result.events) == 3
        assert result.snapshot_sent is False
        assert result.state_snapshot is None
        event_replayer.replay.assert_called_once_with("s1", since_seq=3)

    async def test_replayed_events_are_in_seq_order(self) -> None:
        """Replayed events are sorted in strictly increasing seq order."""
        event_replayer = AsyncMock()
        # Return events out of order
        events = [
            _make_event(session_id="s1", seq=6),
            _make_event(session_id="s1", seq=4),
            _make_event(session_id="s1", seq=5),
        ]
        event_replayer.replay = AsyncMock(return_value=events)

        recovery = _make_recovery(event_replayer=event_replayer)
        result = await recovery.handle_client_reconnect("s1", last_received_seq=3)

        seqs = [e.seq for e in result.events]
        assert seqs == [4, 5, 6]

    async def test_no_replayer_returns_empty_result(self) -> None:
        """Without an event replayer, returns empty result."""
        recovery = _make_recovery(event_replayer=None)
        result = await recovery.handle_client_reconnect("s1", last_received_seq=3)

        assert result.events == []
        assert result.snapshot_sent is False


# --- Test: State snapshot for stale clients (Req 21.6) ---


class TestClientReconnectSnapshot:
    """Tests for state snapshot on stale client reconnect — Req 21.6."""

    async def test_sends_snapshot_when_client_too_far_behind(self) -> None:
        """Sends state snapshot if client's last seq precedes oldest retained event."""
        event_replayer = AsyncMock()
        retained_events = [
            _make_event(session_id="s1", seq=50),
            _make_event(session_id="s1", seq=51),
        ]
        event_replayer.replay = AsyncMock(return_value=retained_events)

        store = InMemoryCheckpointStore()
        recovery = _make_recovery(store=store, event_replayer=event_replayer)

        # Set up state snapshot and oldest retained seq
        snapshot_state = {"session_id": "s1", "status": "running"}
        recovery._last_state_snapshots["s1"] = snapshot_state
        recovery.update_oldest_retained_seq("s1", 50)

        # Client is behind (last seq=10, oldest retained=50)
        result = await recovery.handle_client_reconnect("s1", last_received_seq=10)

        assert result.snapshot_sent is True
        assert result.state_snapshot == snapshot_state
        assert len(result.events) == 2

    async def test_no_snapshot_when_client_is_within_window(self) -> None:
        """Does not send snapshot when client's seq is within retained window."""
        event_replayer = AsyncMock()
        events = [_make_event(session_id="s1", seq=6)]
        event_replayer.replay = AsyncMock(return_value=events)

        recovery = _make_recovery(event_replayer=event_replayer)
        recovery.update_oldest_retained_seq("s1", 3)

        # Client at seq=5, oldest retained=3 — client is within window
        result = await recovery.handle_client_reconnect("s1", last_received_seq=5)

        assert result.snapshot_sent is False
        assert result.state_snapshot is None

    async def test_snapshot_events_are_in_seq_order(self) -> None:
        """Events sent with snapshot are in strictly increasing seq order."""
        event_replayer = AsyncMock()
        retained_events = [
            _make_event(session_id="s1", seq=102),
            _make_event(session_id="s1", seq=100),
            _make_event(session_id="s1", seq=101),
        ]
        event_replayer.replay = AsyncMock(return_value=retained_events)

        recovery = _make_recovery(event_replayer=event_replayer)
        recovery._last_state_snapshots["s1"] = {"status": "running"}
        recovery.update_oldest_retained_seq("s1", 100)

        result = await recovery.handle_client_reconnect("s1", last_received_seq=5)

        seqs = [e.seq for e in result.events]
        assert seqs == [100, 101, 102]


# --- Test: State tracking helpers ---


class TestStateTracking:
    """Tests for state tracking helper methods."""

    def test_record_commit(self) -> None:
        """record_commit tracks committed tasks per session."""
        recovery = _make_recovery()
        recovery.record_commit("s1", "t1")
        recovery.record_commit("s1", "t2")

        assert recovery._committed_tasks["s1"] == {"t1", "t2"}

    def test_register_task_workspace(self) -> None:
        """register_task_workspace maps task to workspace."""
        recovery = _make_recovery()
        recovery.register_task_workspace("t1", "ws-1")

        assert recovery._task_workspaces["t1"] == "ws-1"

    def test_update_oldest_retained_seq(self) -> None:
        """update_oldest_retained_seq tracks window boundary."""
        recovery = _make_recovery()
        recovery.update_oldest_retained_seq("s1", 50)

        assert recovery._oldest_retained_seq["s1"] == 50

    def test_clear_session_removes_all_tracking(self) -> None:
        """clear_session removes committed tasks, snapshots, and seq."""
        recovery = _make_recovery()
        recovery.record_commit("s1", "t1")
        recovery._last_state_snapshots["s1"] = {"status": "ok"}
        recovery.update_oldest_retained_seq("s1", 10)

        recovery.clear_session("s1")

        assert "s1" not in recovery._committed_tasks
        assert "s1" not in recovery._last_state_snapshots
        assert "s1" not in recovery._oldest_retained_seq


# --- Test: Redaction integration ---


class TestRedactionIntegration:
    """Tests for secret redaction during checkpointing."""

    async def test_checkpoint_without_redactor_stores_state_copy(self) -> None:
        """Without a redaction function, state is stored as a copy."""
        store = InMemoryCheckpointStore()
        recovery = _make_recovery(store=store, redact_state=None)

        state = {"session_id": "s1", "status": "running"}
        await recovery.checkpoint_after_node("s1", "plan", state, 5)

        latest = await store.get_latest_checkpoint("s1")
        assert latest is not None
        assert latest.redacted_state == state

    async def test_checkpoint_with_redactor_applies_redaction(self) -> None:
        """With a redaction function, secrets are redacted before storage."""
        store = InMemoryCheckpointStore()
        calls = []

        def redact(state, session_id=None):
            calls.append((state, session_id))
            result = dict(state)
            result["token"] = "***REDACTED***"
            return result

        recovery = _make_recovery(store=store, redact_state=redact)

        state = {"session_id": "s1", "token": "secret-abc"}
        await recovery.checkpoint_after_node("s1", "architect", state, 3)

        latest = await store.get_latest_checkpoint("s1")
        assert latest is not None
        assert latest.redacted_state["token"] == "***REDACTED***"
        assert len(calls) == 1
        assert calls[0][1] == "s1"
