"""Crash Recovery — checkpoint persistence and resume workflow.

Implements checkpointing after each workflow node and a resume procedure
on restart. Handles client reconnection with event replay.

Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState

logger = logging.getLogger(__name__)


# --- Protocols for dependencies ---


class CheckpointStore(Protocol):
    """Protocol for the relational store that persists checkpoints.

    Implementations write to a `checkpoints` table with columns:
    session_id, node_id, highest_seq, redacted_state, status, created_at.
    """

    async def write_checkpoint(
        self,
        session_id: str,
        node_id: str,
        highest_seq: int,
        redacted_state: dict[str, Any],
    ) -> None:
        """Persist a checkpoint record.

        Raises:
            CheckpointWriteError: If the write fails.
        """
        ...

    async def get_latest_checkpoint(self, session_id: str) -> "Checkpoint | None":
        """Retrieve the most recent checkpoint for a session.

        Returns:
            The latest Checkpoint, or None if no checkpoint exists.
        """
        ...

    async def list_non_terminal_sessions(self) -> list["Checkpoint"]:
        """List all sessions whose latest checkpoint references a non-terminal node.

        Returns:
            List of Checkpoint records for sessions that need resuming.
        """
        ...


class EventPublisher(Protocol):
    """Protocol for publishing events to the bus."""

    async def publish(self, event: Event) -> Event: ...


class EventReplayer(Protocol):
    """Protocol for replaying events from the event bus."""

    async def replay(self, correlation_id: str, since_seq: int = 0) -> list[Event]: ...


class WorkspaceDestroyer(Protocol):
    """Protocol for destroying workspaces on recovery."""

    async def destroy(self, workspace_id: str, reason: str) -> None: ...


class TaskRequeuer(Protocol):
    """Protocol for re-queuing tasks within retry budget."""

    async def requeue_task(self, session_id: str, task_id: str) -> bool:
        """Re-queue a task if within retry budget.

        Returns:
            True if re-queued, False if retry budget exhausted.
        """
        ...


# --- Exceptions ---


class CheckpointWriteError(Exception):
    """Raised when a checkpoint write to the relational store fails.

    Per Req 21.2: The workflow halts, emits an error event, and does not advance.
    """

    def __init__(self, session_id: str, node_id: str, reason: str) -> None:
        self.session_id = session_id
        self.node_id = node_id
        self.reason = reason
        super().__init__(
            f"Checkpoint write failed for session={session_id}, "
            f"node={node_id}: {reason}"
        )


class ResumeError(Exception):
    """Raised when a session cannot be resumed from its checkpoint."""

    def __init__(self, session_id: str, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"Resume failed for session={session_id}: {reason}")


# --- Data models ---


# Terminal nodes — sessions at these nodes don't need resuming
TERMINAL_NODES = frozenset({"finalize", "done", "completed", "failed", "cancelled"})


@dataclass(frozen=True)
class Checkpoint:
    """A persisted checkpoint record from the relational store.

    Captures the redacted ForgeState, the completed node identifier,
    and the highest emitted seq at the time of the checkpoint.
    """

    session_id: str
    node_id: str
    highest_seq: int
    redacted_state: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        """Whether this checkpoint is at a terminal (completed) node."""
        return self.node_id in TERMINAL_NODES


@dataclass
class ResumeResult:
    """Result of attempting to resume a session from checkpoint."""

    session_id: str
    node_id: str
    state: dict[str, Any]
    highest_seq: int
    tasks_requeued: list[str] = field(default_factory=list)
    workspaces_destroyed: list[str] = field(default_factory=list)
    success: bool = True
    error: str = ""


@dataclass
class ClientReconnectResult:
    """Result of handling a client reconnection."""

    session_id: str
    events: list[Event] = field(default_factory=list)
    state_snapshot: dict[str, Any] | None = None
    snapshot_sent: bool = False
    from_seq: int = 0


# --- In-memory checkpoint store (for testing and single-process deployment) ---


class InMemoryCheckpointStore:
    """In-memory implementation of CheckpointStore for testing.

    In production this would be backed by PostgreSQL (checkpoints table).
    """

    def __init__(self) -> None:
        # session_id -> list of checkpoints (ordered by creation time)
        self._checkpoints: dict[str, list[Checkpoint]] = {}
        # Inject failures for testing
        self._fail_next_write: bool = False
        self._fail_reason: str = ""

    def set_fail_next_write(self, reason: str = "simulated failure") -> None:
        """Configure the next write to fail (for testing)."""
        self._fail_next_write = True
        self._fail_reason = reason

    async def write_checkpoint(
        self,
        session_id: str,
        node_id: str,
        highest_seq: int,
        redacted_state: dict[str, Any],
    ) -> None:
        """Persist a checkpoint record.

        Raises:
            CheckpointWriteError: If write is configured to fail.
        """
        if self._fail_next_write:
            self._fail_next_write = False
            raise CheckpointWriteError(session_id, node_id, self._fail_reason)

        checkpoint = Checkpoint(
            session_id=session_id,
            node_id=node_id,
            highest_seq=highest_seq,
            redacted_state=redacted_state,
            status="active",
        )

        if session_id not in self._checkpoints:
            self._checkpoints[session_id] = []
        self._checkpoints[session_id].append(checkpoint)

    async def get_latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        """Retrieve the most recent checkpoint for a session."""
        checkpoints = self._checkpoints.get(session_id, [])
        if not checkpoints:
            return None
        return checkpoints[-1]

    async def list_non_terminal_sessions(self) -> list[Checkpoint]:
        """List sessions whose latest checkpoint is non-terminal."""
        results: list[Checkpoint] = []
        for session_id, checkpoints in self._checkpoints.items():
            if checkpoints:
                latest = checkpoints[-1]
                if not latest.is_terminal:
                    results.append(latest)
        return results

    def clear(self) -> None:
        """Clear all stored checkpoints."""
        self._checkpoints.clear()

    def get_all(self, session_id: str) -> list[Checkpoint]:
        """Get all checkpoints for a session (for testing)."""
        return list(self._checkpoints.get(session_id, []))


# --- CrashRecovery component ---


class CrashRecovery:
    """Crash Recovery component for the Forge Runtime.

    Provides:
    1. Checkpoint persistence after each workflow node (Req 21.1)
    2. Halt on checkpoint write failure (Req 21.2)
    3. Resume from last completed node on restart (Req 21.3)
    4. Workspace destruction and task re-queuing for uncommitted tasks (Req 21.4)
    5. Event replay on client reconnect (Req 21.5)
    6. State snapshot + retained events for stale clients (Req 21.6)

    Usage:
        recovery = CrashRecovery(
            checkpoint_store=store,
            event_bus=bus,
            secret_holder=secret_holder,
        )

        # After each node completes:
        await recovery.checkpoint_after_node(session_id, node_id, state, highest_seq)

        # On restart:
        results = await recovery.resume_all()

        # On client reconnect:
        result = await recovery.handle_client_reconnect(session_id, last_seq)
    """

    def __init__(
        self,
        *,
        checkpoint_store: CheckpointStore,
        event_bus: EventPublisher | None = None,
        event_replayer: EventReplayer | None = None,
        workspace_destroyer: WorkspaceDestroyer | None = None,
        task_requeuer: TaskRequeuer | None = None,
        redact_state: Any | None = None,
        retained_event_window: int = 1000,
    ) -> None:
        """Initialize the CrashRecovery component.

        Args:
            checkpoint_store: Relational store for checkpoint persistence.
            event_bus: EventBus for publishing error events.
            event_replayer: For replaying events on reconnect.
            workspace_destroyer: For destroying workspaces during recovery.
            task_requeuer: For re-queuing tasks within retry budget.
            redact_state: Optional callable that redacts secrets from state.
                          Signature: (state: dict) -> dict
            retained_event_window: Number of events retained for replay (Req 21.6).
        """
        self._store = checkpoint_store
        self._event_bus = event_bus
        self._event_replayer = event_replayer
        self._workspace_destroyer = workspace_destroyer
        self._task_requeuer = task_requeuer
        self._redact_state = redact_state
        self._retained_event_window = retained_event_window

        # Track committed tasks per session for crash recovery (Req 21.4)
        self._committed_tasks: dict[str, set[str]] = {}

        # Track active workspaces per task for cleanup
        self._task_workspaces: dict[str, str] = {}  # task_id -> workspace_id

        # Last known state per session (for snapshot on reconnect, Req 21.6)
        self._last_state_snapshots: dict[str, dict[str, Any]] = {}

        # Oldest retained seq per session (for determining if snapshot needed)
        self._oldest_retained_seq: dict[str, int] = {}

    # --- Req 21.1: Checkpoint after each node ---

    async def checkpoint_after_node(
        self,
        session_id: str,
        node_id: str,
        state: dict[str, Any],
        highest_seq: int,
    ) -> None:
        """Checkpoint redacted ForgeState, completed node ID, and highest seq.

        Called after each workflow node completes, before the next node begins.

        Per Req 21.1: Persists the redacted state to the relational store.
        Per Req 21.2: On write failure, halts (raises), emits error, does not advance.

        Args:
            session_id: The session being checkpointed.
            node_id: The node that just completed.
            state: The current ForgeState (will be redacted before persistence).
            highest_seq: The highest event seq emitted so far.

        Raises:
            CheckpointWriteError: If the checkpoint cannot be written.
        """
        # Redact secrets from state before persistence
        redacted_state = self._redact(state, session_id)

        try:
            await self._store.write_checkpoint(
                session_id=session_id,
                node_id=node_id,
                highest_seq=highest_seq,
                redacted_state=redacted_state,
            )
        except CheckpointWriteError:
            # Req 21.2: Emit error event and re-raise to halt
            await self._emit_checkpoint_error(session_id, node_id)
            raise
        except Exception as exc:
            # Wrap unexpected errors as CheckpointWriteError
            await self._emit_checkpoint_error(session_id, node_id)
            raise CheckpointWriteError(
                session_id, node_id, str(exc)
            ) from exc

        # Update last state snapshot for reconnect support
        self._last_state_snapshots[session_id] = redacted_state

        logger.info(
            "Checkpoint written: session=%s, node=%s, seq=%d",
            session_id,
            node_id,
            highest_seq,
        )

    # --- Req 21.3: Resume on restart ---

    async def resume_all(self) -> list[ResumeResult]:
        """Identify and resume all sessions with non-terminal checkpoints.

        Called on runtime restart. Identifies sessions that were interrupted
        and resumes each from its last completed node.

        Per Req 21.3: Resume from last completed node using persisted state.
        Per Req 21.4: Destroy workspace and re-queue task if no commit.done.

        Returns:
            List of ResumeResult for each resumed session.
        """
        non_terminal = await self._store.list_non_terminal_sessions()
        results: list[ResumeResult] = []

        for checkpoint in non_terminal:
            result = await self._resume_session(checkpoint)
            results.append(result)

        if results:
            logger.info(
                "Resumed %d sessions on restart",
                len([r for r in results if r.success]),
            )

        return results

    async def _resume_session(self, checkpoint: Checkpoint) -> ResumeResult:
        """Resume a single session from its last checkpoint.

        Handles workspace cleanup for tasks that hadn't committed.

        Args:
            checkpoint: The checkpoint to resume from.

        Returns:
            ResumeResult describing the outcome.
        """
        session_id = checkpoint.session_id
        result = ResumeResult(
            session_id=session_id,
            node_id=checkpoint.node_id,
            state=checkpoint.redacted_state,
            highest_seq=checkpoint.highest_seq,
        )

        try:
            # Req 21.4: Handle uncommitted tasks
            await self._recover_uncommitted_tasks(session_id, checkpoint, result)
        except Exception as exc:
            result.success = False
            result.error = f"Recovery failed: {exc}"
            logger.error(
                "Failed to resume session %s: %s", session_id, exc
            )

        return result

    async def _recover_uncommitted_tasks(
        self,
        session_id: str,
        checkpoint: Checkpoint,
        result: ResumeResult,
    ) -> None:
        """Destroy workspaces and re-queue tasks that didn't commit before crash.

        Per Req 21.4: If a task had not emitted commit.done at crash time,
        destroy its workspace and re-queue within retry budget.

        Args:
            session_id: The session being recovered.
            checkpoint: The checkpoint state.
            result: The result to update with recovery actions.
        """
        state = checkpoint.redacted_state
        committed = self._committed_tasks.get(session_id, set())

        # Find tasks that were active but not committed
        tasks = state.get("tasks", [])
        for task in tasks:
            task_id = task.get("id", "") if isinstance(task, dict) else ""
            if not task_id:
                continue

            task_status = task.get("status", "") if isinstance(task, dict) else ""

            # If task was running/verifying but never committed
            if task_status in ("running", "verifying") and task_id not in committed:
                # Destroy workspace if we have a destroyer
                workspace_id = self._task_workspaces.get(task_id)
                if workspace_id and self._workspace_destroyer:
                    try:
                        await self._workspace_destroyer.destroy(
                            workspace_id, "crash_recovery"
                        )
                        result.workspaces_destroyed.append(workspace_id)
                        # Emit workspace.destroyed event
                        await self._emit_workspace_destroyed(
                            session_id, workspace_id, task_id
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to destroy workspace %s: %s",
                            workspace_id,
                            exc,
                        )

                # Re-queue task within retry budget
                if self._task_requeuer:
                    try:
                        requeued = await self._task_requeuer.requeue_task(
                            session_id, task_id
                        )
                        if requeued:
                            result.tasks_requeued.append(task_id)
                    except Exception as exc:
                        logger.warning(
                            "Failed to requeue task %s: %s", task_id, exc
                        )

    # --- Req 21.5: Replay missed events on client reconnect ---

    async def handle_client_reconnect(
        self,
        session_id: str,
        last_received_seq: int,
    ) -> ClientReconnectResult:
        """Handle client reconnection by replaying missed events.

        Per Req 21.5: Replay events missed since client's last received seq.
        Per Req 21.6: Send state snapshot + retained events if client's last seq
                      precedes the oldest retained event.

        Args:
            session_id: The session the client is reconnecting to.
            last_received_seq: The last seq the client received before disconnect.

        Returns:
            ClientReconnectResult with events to send and optional state snapshot.
        """
        result = ClientReconnectResult(
            session_id=session_id,
            from_seq=last_received_seq,
        )

        if not self._event_replayer:
            return result

        # Get the oldest retained event seq for this session
        oldest_retained = self._oldest_retained_seq.get(session_id, 1)

        # Req 21.6: If client's last seq precedes oldest retained event,
        # send state snapshot + retained events
        if last_received_seq < oldest_retained:
            # Need to send snapshot because we can't replay from that far back
            snapshot = self._last_state_snapshots.get(session_id)
            if snapshot:
                result.state_snapshot = snapshot
                result.snapshot_sent = True

            # Replay all retained events (from oldest retained onward)
            events = await self._event_replayer.replay(
                session_id, since_seq=oldest_retained - 1
            )
            result.events = sorted(events, key=lambda e: e.seq)
        else:
            # Req 21.5: Replay events since client's last_received_seq in seq order
            events = await self._event_replayer.replay(
                session_id, since_seq=last_received_seq
            )
            result.events = sorted(events, key=lambda e: e.seq)

        return result

    # --- State tracking helpers ---

    def record_commit(self, session_id: str, task_id: str) -> None:
        """Record that a task has successfully committed.

        Called when a commit.done event is observed for a task. Used during
        crash recovery to determine which tasks need re-queuing.

        Args:
            session_id: The session the task belongs to.
            task_id: The task that committed.
        """
        if session_id not in self._committed_tasks:
            self._committed_tasks[session_id] = set()
        self._committed_tasks[session_id].add(task_id)

    def register_task_workspace(self, task_id: str, workspace_id: str) -> None:
        """Register a workspace assignment for a task.

        Used during crash recovery to know which workspaces to destroy.

        Args:
            task_id: The task being assigned the workspace.
            workspace_id: The workspace assigned to the task.
        """
        self._task_workspaces[task_id] = workspace_id

    def update_oldest_retained_seq(self, session_id: str, oldest_seq: int) -> None:
        """Update the oldest retained event seq for a session.

        Called when events are pruned from the event bus's retained window.

        Args:
            session_id: The session whose window is being updated.
            oldest_seq: The seq of the oldest still-retained event.
        """
        self._oldest_retained_seq[session_id] = oldest_seq

    def clear_session(self, session_id: str) -> None:
        """Clear all recovery state for a terminated session.

        Args:
            session_id: The session to clean up.
        """
        self._committed_tasks.pop(session_id, None)
        self._last_state_snapshots.pop(session_id, None)
        self._oldest_retained_seq.pop(session_id, None)
        # Clean up task workspace mappings for this session
        # (we don't track session->task mapping here, but workspaces
        # will be cleaned up by the workspace manager)

    # --- Private helpers ---

    def _redact(self, state: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Redact secrets from state before checkpoint persistence.

        Uses the provided redact_state callable if available, otherwise
        returns a copy of the state (assumes already redacted).

        Args:
            state: The ForgeState dict to redact.
            session_id: The session for secret lookup.

        Returns:
            A redacted copy of the state safe for persistence.
        """
        if self._redact_state:
            return self._redact_state(state, session_id=session_id)
        # If no redaction function provided, return a shallow copy
        return dict(state)

    async def _emit_checkpoint_error(self, session_id: str, node_id: str) -> None:
        """Emit an error event for a checkpoint write failure.

        Per Req 21.2: Emit error event on checkpoint failure.

        Args:
            session_id: The session that failed to checkpoint.
            node_id: The node where the failure occurred.
        """
        if not self._event_bus:
            return

        try:
            error_event = Event.create(
                type=EventType.ERROR,
                session_id=session_id,
                source="crash_recovery",
                payload={
                    "where": "checkpoint",
                    "error_type": "checkpoint_write_failure",
                    "message": f"Failed to write checkpoint at node '{node_id}'",
                    "recoverable": False,
                    "node_id": node_id,
                },
            )
            await self._event_bus.publish(error_event)
        except Exception as exc:
            # Don't let error emission failure mask the original checkpoint error
            logger.warning(
                "Failed to emit checkpoint error event for session=%s: %s",
                session_id,
                exc,
            )

    async def _emit_workspace_destroyed(
        self, session_id: str, workspace_id: str, task_id: str
    ) -> None:
        """Emit a workspace.destroyed event during crash recovery.

        Per Req 21.4: Emit workspace.destroyed when destroying workspace
        for uncommitted task on recovery.

        Args:
            session_id: The session owning the workspace.
            workspace_id: The workspace being destroyed.
            task_id: The task that owned the workspace.
        """
        if not self._event_bus:
            return

        try:
            event = Event.create(
                type=EventType.WORKSPACE_DESTROYED,
                session_id=session_id,
                source="crash_recovery",
                payload={
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "reason": "crash_recovery",
                },
            )
            await self._event_bus.publish(event)
        except Exception as exc:
            logger.warning(
                "Failed to emit workspace.destroyed for %s: %s",
                workspace_id,
                exc,
            )
