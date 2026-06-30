"""Workspace Manager - isolated sandboxed repository copies per task.

Creates, destroys, and tracks isolated workspace sandboxes. Each workspace
is a temporary directory representing an isolated copy of the repository
at the session's base ref. Workspaces are owned by a single task and are
destroyed on task completion or merge.

Requirements: 6.1, 6.2, 6.3, 6.6, 6.7
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.runtime.events.bus import EventBus
from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)

# Default maximum workspace age in seconds before orphan reaping
DEFAULT_MAX_WORKSPACE_AGE = 3600


class WorkspaceStatus(str, Enum):
    """Status of a managed workspace."""

    ACTIVE = "active"
    DESTROYED = "destroyed"


@dataclass
class WorkspaceInfo:
    """Metadata tracking a single managed workspace."""

    workspace_id: str
    task_id: str
    session_id: str
    path: str
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    created_at: float = field(default_factory=time.time)


class WorkspaceCreationError(Exception):
    """Raised when workspace creation fails."""

    def __init__(self, reason: str, task_id: str, session_id: str) -> None:
        self.reason = reason
        self.task_id = task_id
        self.session_id = session_id
        super().__init__(f"Failed to create workspace for task {task_id}: {reason}")


class WorkspaceManager:
    """Manages isolated sandboxed repository copies per task.

    Each workspace is a temporary directory that represents an isolated
    copy of the repository at the session base ref. The manager tracks
    workspace metadata and emits lifecycle events via the EventBus.

    Workspace lifecycle:
    1. create() — allocate a sandbox, emit workspace.created
    2. destroy() — clean up sandbox, emit workspace.destroyed
    3. reap_orphans() — destroy workspaces exceeding max age whose task is inactive
    """

    def __init__(
        self,
        event_bus: EventBus,
        base_dir: str | None = None,
        max_workspace_age: int = DEFAULT_MAX_WORKSPACE_AGE,
    ) -> None:
        """Initialize the WorkspaceManager.

        Args:
            event_bus: The event bus for publishing workspace lifecycle events.
            base_dir: Base directory for creating workspace temp dirs.
                      If None, uses system temp directory.
            max_workspace_age: Maximum age in seconds before a workspace is
                               considered orphaned and eligible for reaping.
        """
        self._event_bus = event_bus
        self._base_dir = base_dir
        self._max_workspace_age = max_workspace_age
        self._workspaces: dict[str, WorkspaceInfo] = {}

    @property
    def max_workspace_age(self) -> int:
        """The configured maximum workspace age in seconds."""
        return self._max_workspace_age

    def get_workspace(self, workspace_id: str) -> WorkspaceInfo | None:
        """Get workspace metadata by ID.

        Args:
            workspace_id: The workspace identifier.

        Returns:
            WorkspaceInfo if found and active, None otherwise.
        """
        info = self._workspaces.get(workspace_id)
        if info and info.status == WorkspaceStatus.ACTIVE:
            return info
        return None

    def list_workspaces(self, session_id: str | None = None) -> list[WorkspaceInfo]:
        """List all active workspaces, optionally filtered by session.

        Args:
            session_id: If provided, only return workspaces for this session.

        Returns:
            List of active WorkspaceInfo entries.
        """
        results = [
            ws for ws in self._workspaces.values()
            if ws.status == WorkspaceStatus.ACTIVE
        ]
        if session_id is not None:
            results = [ws for ws in results if ws.session_id == session_id]
        return results

    async def create(
        self,
        task_id: str,
        session_id: str,
        base_ref: str = "main",
    ) -> WorkspaceInfo:
        """Create a sandboxed workspace for a task.

        Creates a temporary directory representing the isolated workspace,
        records metadata, and emits a workspace.created event.

        Args:
            task_id: The owning task's identifier.
            session_id: The session this workspace belongs to.
            base_ref: The repository ref to base the workspace on.

        Returns:
            WorkspaceInfo with the new workspace's metadata.

        Raises:
            WorkspaceCreationError: If workspace creation fails.
        """
        workspace_id = str(uuid.uuid4())

        try:
            # Create the sandboxed directory
            workspace_path = tempfile.mkdtemp(
                prefix=f"forge_ws_{task_id[:8]}_",
                dir=self._base_dir,
            )
        except (OSError, PermissionError) as e:
            # Emit creation-failure event
            await self._emit_creation_failure(
                workspace_id=workspace_id,
                task_id=task_id,
                session_id=session_id,
                reason=str(e),
            )
            raise WorkspaceCreationError(
                reason=str(e),
                task_id=task_id,
                session_id=session_id,
            ) from e

        # Record workspace metadata
        info = WorkspaceInfo(
            workspace_id=workspace_id,
            task_id=task_id,
            session_id=session_id,
            path=workspace_path,
            status=WorkspaceStatus.ACTIVE,
        )
        self._workspaces[workspace_id] = info

        # Emit workspace.created event
        await self._emit_created(info)

        logger.info(
            "Created workspace %s for task %s (session %s) at %s",
            workspace_id,
            task_id,
            session_id,
            workspace_path,
        )

        return info

    async def destroy(
        self,
        workspace_id: str,
        reason: str = "task_complete",
    ) -> bool:
        """Destroy a workspace and clean up its sandbox directory.

        Removes the workspace directory, marks metadata as destroyed,
        and emits a workspace.destroyed event.

        Args:
            workspace_id: The workspace to destroy.
            reason: The reason for destruction (e.g. "task_complete",
                    "merged", "orphan-expiry").

        Returns:
            True if the workspace was destroyed, False if not found or
            already destroyed.
        """
        info = self._workspaces.get(workspace_id)
        if info is None or info.status == WorkspaceStatus.DESTROYED:
            return False

        # Clean up the directory
        workspace_path = Path(info.path)
        if workspace_path.exists():
            try:
                shutil.rmtree(workspace_path)
            except OSError as e:
                logger.warning(
                    "Failed to remove workspace directory %s: %s",
                    info.path,
                    e,
                )

        # Mark as destroyed
        info.status = WorkspaceStatus.DESTROYED

        # Emit workspace.destroyed event
        await self._emit_destroyed(info, reason)

        logger.info(
            "Destroyed workspace %s (task %s, reason: %s)",
            workspace_id,
            info.task_id,
            reason,
        )

        return True

    async def reap_orphans(
        self,
        active_task_ids: set[str] | None = None,
    ) -> list[str]:
        """Reap orphaned workspaces exceeding the configured max age.

        A workspace is considered orphaned if:
        1. Its owning task is no longer in the active set, AND
        2. Its age exceeds the configured max_workspace_age.

        Args:
            active_task_ids: Set of currently active task IDs.
                             If None, all workspaces with expired age are reaped.

        Returns:
            List of workspace IDs that were reaped.
        """
        if active_task_ids is None:
            active_task_ids = set()

        now = time.time()
        reaped: list[str] = []

        # Collect candidates (avoid modifying dict during iteration)
        candidates = [
            ws for ws in self._workspaces.values()
            if ws.status == WorkspaceStatus.ACTIVE
            and ws.task_id not in active_task_ids
            and (now - ws.created_at) > self._max_workspace_age
        ]

        for ws in candidates:
            destroyed = await self.destroy(ws.workspace_id, reason="orphan-expiry")
            if destroyed:
                reaped.append(ws.workspace_id)

        if reaped:
            logger.info("Reaped %d orphaned workspaces: %s", len(reaped), reaped)

        return reaped

    async def destroy_session_workspaces(self, session_id: str) -> list[str]:
        """Destroy all workspaces belonging to a session.

        Used during session deletion to clean up all associated workspaces.

        Args:
            session_id: The session whose workspaces to destroy.

        Returns:
            List of destroyed workspace IDs.
        """
        destroyed_ids: list[str] = []
        candidates = [
            ws for ws in self._workspaces.values()
            if ws.session_id == session_id and ws.status == WorkspaceStatus.ACTIVE
        ]
        for ws in candidates:
            success = await self.destroy(ws.workspace_id, reason="session_deleted")
            if success:
                destroyed_ids.append(ws.workspace_id)
        return destroyed_ids

    # ─────────────────────────────────────────────────────────────────────────
    # Event emission helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _emit_created(self, info: WorkspaceInfo) -> None:
        """Emit a workspace.created event."""
        event = Event.create(
            type=EventType.WORKSPACE_CREATED,
            session_id=info.session_id,
            source="workspace_manager",
            payload={
                "workspace_id": info.workspace_id,
                "task_id": info.task_id,
                "path": info.path,
            },
            correlation_id=info.session_id,
            event_id=f"ws-created-{info.workspace_id}",
        )
        await self._event_bus.publish(event)

    async def _emit_destroyed(self, info: WorkspaceInfo, reason: str) -> None:
        """Emit a workspace.destroyed event."""
        event = Event.create(
            type=EventType.WORKSPACE_DESTROYED,
            session_id=info.session_id,
            source="workspace_manager",
            payload={
                "workspace_id": info.workspace_id,
                "task_id": info.task_id,
                "reason": reason,
            },
            correlation_id=info.session_id,
            event_id=f"ws-destroyed-{info.workspace_id}",
        )
        await self._event_bus.publish(event)

    async def _emit_creation_failure(
        self,
        workspace_id: str,
        task_id: str,
        session_id: str,
        reason: str,
    ) -> None:
        """Emit a workspace creation failure event (error type)."""
        event = Event.create(
            type=EventType.ERROR,
            session_id=session_id,
            source="workspace_manager",
            payload={
                "error": "workspace_creation_failed",
                "workspace_id": workspace_id,
                "task_id": task_id,
                "reason": reason,
            },
            correlation_id=session_id,
            event_id=f"ws-fail-{workspace_id}",
        )
        await self._event_bus.publish(event)
