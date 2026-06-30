"""Unit tests for WorkspaceManager with create/destroy/merge operations.

Tests workspace creation, destruction, orphan reaping, event emission,
and error handling.

Requirements: 6.1, 6.2, 6.3, 6.6, 6.7
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.runtime.events.bus import EventBus
from app.runtime.events.models import EventType
from app.runtime.workspace import (
    DEFAULT_MAX_WORKSPACE_AGE,
    WorkspaceCreationError,
    WorkspaceInfo,
    WorkspaceManager,
    WorkspaceStatus,
)


@pytest.fixture
def event_bus() -> EventBus:
    """A fresh EventBus for testing."""
    return EventBus()


@pytest.fixture
def manager(event_bus: EventBus, tmp_path: Path) -> WorkspaceManager:
    """A WorkspaceManager using a temp directory as base."""
    return WorkspaceManager(
        event_bus=event_bus,
        base_dir=str(tmp_path),
        max_workspace_age=DEFAULT_MAX_WORKSPACE_AGE,
    )


@pytest.fixture
def short_age_manager(event_bus: EventBus, tmp_path: Path) -> WorkspaceManager:
    """A WorkspaceManager with a very short max age for reap testing."""
    return WorkspaceManager(
        event_bus=event_bus,
        base_dir=str(tmp_path),
        max_workspace_age=1,  # 1 second for testing
    )


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 6.2: Create sandboxed workspace copy at session base ref
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceCreate:
    """Test workspace creation lifecycle."""

    async def test_create_returns_workspace_info(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(
            task_id="task-1",
            session_id="session-1",
            base_ref="main",
        )
        assert isinstance(info, WorkspaceInfo)
        assert info.task_id == "task-1"
        assert info.session_id == "session-1"
        assert info.status == WorkspaceStatus.ACTIVE

    async def test_create_assigns_unique_workspace_id(
        self, manager: WorkspaceManager
    ) -> None:
        ws1 = await manager.create(task_id="task-1", session_id="session-1")
        ws2 = await manager.create(task_id="task-2", session_id="session-1")
        assert ws1.workspace_id != ws2.workspace_id

    async def test_create_creates_directory(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        assert Path(info.path).exists()
        assert Path(info.path).is_dir()

    async def test_create_records_creation_time(
        self, manager: WorkspaceManager
    ) -> None:
        before = time.time()
        info = await manager.create(task_id="task-1", session_id="session-1")
        after = time.time()
        assert before <= info.created_at <= after

    async def test_get_workspace_returns_active(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        retrieved = manager.get_workspace(info.workspace_id)
        assert retrieved is not None
        assert retrieved.workspace_id == info.workspace_id

    async def test_get_workspace_nonexistent_returns_none(
        self, manager: WorkspaceManager
    ) -> None:
        assert manager.get_workspace("nonexistent") is None

    async def test_list_workspaces_empty(self, manager: WorkspaceManager) -> None:
        assert manager.list_workspaces() == []

    async def test_list_workspaces_returns_active(
        self, manager: WorkspaceManager
    ) -> None:
        await manager.create(task_id="task-1", session_id="session-1")
        await manager.create(task_id="task-2", session_id="session-1")
        workspaces = manager.list_workspaces()
        assert len(workspaces) == 2

    async def test_list_workspaces_filters_by_session(
        self, manager: WorkspaceManager
    ) -> None:
        await manager.create(task_id="task-1", session_id="session-1")
        await manager.create(task_id="task-2", session_id="session-2")
        s1_workspaces = manager.list_workspaces(session_id="session-1")
        assert len(s1_workspaces) == 1
        assert s1_workspaces[0].session_id == "session-1"


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 6.2: Emit workspace.created event with workspace ID and owning task
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceCreatedEvent:
    """Test workspace.created event emission."""

    async def test_create_emits_workspace_created_event(
        self, manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")

        events = await event_bus.replay("session-1")
        assert len(events) == 1
        assert events[0].type == EventType.WORKSPACE_CREATED

    async def test_created_event_has_workspace_id(
        self, manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")

        events = await event_bus.replay("session-1")
        payload = events[0].payload
        assert payload["workspace_id"] == info.workspace_id

    async def test_created_event_has_task_id(
        self, manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        await manager.create(task_id="task-42", session_id="session-1")

        events = await event_bus.replay("session-1")
        payload = events[0].payload
        assert payload["task_id"] == "task-42"

    async def test_created_event_source_is_workspace_manager(
        self, manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        await manager.create(task_id="task-1", session_id="session-1")

        events = await event_bus.replay("session-1")
        assert events[0].source == "workspace_manager"


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 6.3: Emit workspace-creation-failure event on error
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceCreationFailure:
    """Test workspace creation failure behavior."""

    async def test_creation_failure_raises_error(
        self, event_bus: EventBus
    ) -> None:
        # Use a non-existent base directory to trigger creation failure
        mgr = WorkspaceManager(
            event_bus=event_bus,
            base_dir="/nonexistent/path/that/does/not/exist",
        )
        with pytest.raises(WorkspaceCreationError) as exc_info:
            await mgr.create(task_id="task-1", session_id="session-1")
        assert exc_info.value.task_id == "task-1"
        assert exc_info.value.session_id == "session-1"

    async def test_creation_failure_emits_error_event(
        self, event_bus: EventBus
    ) -> None:
        mgr = WorkspaceManager(
            event_bus=event_bus,
            base_dir="/nonexistent/path/that/does/not/exist",
        )
        try:
            await mgr.create(task_id="task-1", session_id="session-1")
        except WorkspaceCreationError:
            pass

        events = await event_bus.replay("session-1")
        assert len(events) == 1
        assert events[0].type == EventType.ERROR
        assert events[0].payload["error"] == "workspace_creation_failed"
        assert events[0].payload["task_id"] == "task-1"
        assert "reason" in events[0].payload

    async def test_creation_failure_does_not_register_workspace(
        self, event_bus: EventBus
    ) -> None:
        mgr = WorkspaceManager(
            event_bus=event_bus,
            base_dir="/nonexistent/path/that/does/not/exist",
        )
        try:
            await mgr.create(task_id="task-1", session_id="session-1")
        except WorkspaceCreationError:
            pass

        assert mgr.list_workspaces() == []


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 6.6: Destroy workspace on task completion or merge
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceDestroy:
    """Test workspace destruction lifecycle."""

    async def test_destroy_removes_directory(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        assert Path(info.path).exists()

        await manager.destroy(info.workspace_id, reason="task_complete")
        assert not Path(info.path).exists()

    async def test_destroy_marks_status_destroyed(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        await manager.destroy(info.workspace_id, reason="task_complete")

        # Direct dict access to check internal state
        assert manager._workspaces[info.workspace_id].status == WorkspaceStatus.DESTROYED

    async def test_destroy_returns_true_on_success(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        result = await manager.destroy(info.workspace_id, reason="merged")
        assert result is True

    async def test_destroy_returns_false_for_nonexistent(
        self, manager: WorkspaceManager
    ) -> None:
        result = await manager.destroy("nonexistent-id", reason="task_complete")
        assert result is False

    async def test_destroy_returns_false_for_already_destroyed(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        await manager.destroy(info.workspace_id, reason="task_complete")
        # Second destroy should be no-op
        result = await manager.destroy(info.workspace_id, reason="task_complete")
        assert result is False

    async def test_get_workspace_returns_none_after_destroy(
        self, manager: WorkspaceManager
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        await manager.destroy(info.workspace_id, reason="task_complete")
        assert manager.get_workspace(info.workspace_id) is None

    async def test_destroy_emits_workspace_destroyed_event(
        self, manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")
        await manager.destroy(info.workspace_id, reason="task_complete")

        events = await event_bus.replay("session-1")
        destroyed_events = [
            e for e in events if e.type == EventType.WORKSPACE_DESTROYED
        ]
        assert len(destroyed_events) == 1
        assert destroyed_events[0].payload["workspace_id"] == info.workspace_id
        assert destroyed_events[0].payload["reason"] == "task_complete"

    async def test_destroy_event_carries_task_id(
        self, manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        info = await manager.create(task_id="task-99", session_id="session-1")
        await manager.destroy(info.workspace_id, reason="merged")

        events = await event_bus.replay("session-1")
        destroyed_events = [
            e for e in events if e.type == EventType.WORKSPACE_DESTROYED
        ]
        assert destroyed_events[0].payload["task_id"] == "task-99"
        assert destroyed_events[0].payload["reason"] == "merged"


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 6.7: Reap orphaned workspaces exceeding configured max age
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceReapOrphans:
    """Test orphaned workspace reaping."""

    async def test_reap_orphans_destroys_expired_inactive(
        self, short_age_manager: WorkspaceManager
    ) -> None:
        info = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        # Manually backdate the creation time
        info.created_at = time.time() - 10  # 10 seconds ago (exceeds 1s max age)

        reaped = await short_age_manager.reap_orphans(active_task_ids=set())
        assert info.workspace_id in reaped

    async def test_reap_orphans_skips_active_tasks(
        self, short_age_manager: WorkspaceManager
    ) -> None:
        info = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        info.created_at = time.time() - 10

        # task-1 is still active — should not be reaped
        reaped = await short_age_manager.reap_orphans(
            active_task_ids={"task-1"}
        )
        assert reaped == []

    async def test_reap_orphans_skips_young_workspaces(
        self, short_age_manager: WorkspaceManager
    ) -> None:
        info = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        # Don't backdate — workspace is fresh (created just now)

        reaped = await short_age_manager.reap_orphans(active_task_ids=set())
        assert reaped == []

    async def test_reap_orphans_emits_destroyed_with_expiry_reason(
        self, short_age_manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        info = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        info.created_at = time.time() - 10

        await short_age_manager.reap_orphans(active_task_ids=set())

        events = await event_bus.replay("session-1")
        destroyed_events = [
            e for e in events if e.type == EventType.WORKSPACE_DESTROYED
        ]
        assert len(destroyed_events) == 1
        assert destroyed_events[0].payload["reason"] == "orphan-expiry"

    async def test_reap_orphans_multiple(
        self, short_age_manager: WorkspaceManager
    ) -> None:
        ws1 = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        ws2 = await short_age_manager.create(
            task_id="task-2", session_id="session-1"
        )
        ws3 = await short_age_manager.create(
            task_id="task-3", session_id="session-1"
        )
        # Backdate only ws1 and ws3
        ws1.created_at = time.time() - 10
        ws3.created_at = time.time() - 10

        reaped = await short_age_manager.reap_orphans(active_task_ids=set())
        assert len(reaped) == 2
        assert ws1.workspace_id in reaped
        assert ws3.workspace_id in reaped
        # ws2 should still be active
        assert short_age_manager.get_workspace(ws2.workspace_id) is not None

    async def test_reap_orphans_returns_empty_when_none_eligible(
        self, manager: WorkspaceManager
    ) -> None:
        await manager.create(task_id="task-1", session_id="session-1")
        # Default max age is 3600s, workspace is fresh
        reaped = await manager.reap_orphans(active_task_ids=set())
        assert reaped == []

    async def test_reap_orphans_with_none_active_tasks(
        self, short_age_manager: WorkspaceManager
    ) -> None:
        """When active_task_ids is None, treat as empty set."""
        info = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        info.created_at = time.time() - 10

        reaped = await short_age_manager.reap_orphans(active_task_ids=None)
        assert info.workspace_id in reaped


# ─────────────────────────────────────────────────────────────────────────────
# Session workspace cleanup
# ─────────────────────────────────────────────────────────────────────────────


class TestDestroySessionWorkspaces:
    """Test destroying all workspaces for a session."""

    async def test_destroy_session_workspaces(
        self, manager: WorkspaceManager
    ) -> None:
        await manager.create(task_id="task-1", session_id="session-1")
        await manager.create(task_id="task-2", session_id="session-1")
        await manager.create(task_id="task-3", session_id="session-2")

        destroyed = await manager.destroy_session_workspaces("session-1")
        assert len(destroyed) == 2
        # session-2 workspace is untouched
        assert len(manager.list_workspaces(session_id="session-2")) == 1

    async def test_destroy_session_workspaces_empty(
        self, manager: WorkspaceManager
    ) -> None:
        destroyed = await manager.destroy_session_workspaces("nonexistent")
        assert destroyed == []


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceConfiguration:
    """Test workspace manager configuration."""

    async def test_default_max_age(self, event_bus: EventBus, tmp_path: Path) -> None:
        mgr = WorkspaceManager(event_bus=event_bus, base_dir=str(tmp_path))
        assert mgr.max_workspace_age == DEFAULT_MAX_WORKSPACE_AGE

    async def test_custom_max_age(self, event_bus: EventBus, tmp_path: Path) -> None:
        mgr = WorkspaceManager(
            event_bus=event_bus,
            base_dir=str(tmp_path),
            max_workspace_age=7200,
        )
        assert mgr.max_workspace_age == 7200


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 6.6: Destroy exactly once — additional edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestDestroyExactlyOnce:
    """Requirement 6.6: workspace is destroyed exactly once."""

    async def test_reap_does_not_double_destroy(
        self, short_age_manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        """Calling reap_orphans twice does not destroy the same workspace again."""
        info = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        info.created_at = time.time() - 10

        reaped_first = await short_age_manager.reap_orphans(active_task_ids=set())
        reaped_second = await short_age_manager.reap_orphans(active_task_ids=set())

        assert info.workspace_id in reaped_first
        assert reaped_second == []

        # Only one destroyed event emitted (besides the created event)
        events = await event_bus.replay("session-1")
        destroyed_events = [
            e for e in events if e.type == EventType.WORKSPACE_DESTROYED
        ]
        assert len(destroyed_events) == 1

    async def test_manual_destroy_then_reap_no_duplicate(
        self, short_age_manager: WorkspaceManager
    ) -> None:
        """A manually destroyed workspace is not re-destroyed by reap."""
        info = await short_age_manager.create(
            task_id="task-1", session_id="session-1"
        )
        info.created_at = time.time() - 10

        # Destroy manually first
        await short_age_manager.destroy(info.workspace_id, reason="task_complete")

        # Reap should find nothing
        reaped = await short_age_manager.reap_orphans(active_task_ids=set())
        assert reaped == []


# ─────────────────────────────────────────────────────────────────────────────
# Requirement 6.2: workspace.created event carries workspace path
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspaceCreatedEventPath:
    """Requirement 6.2: workspace.created event includes the workspace path."""

    async def test_created_event_has_path(
        self, manager: WorkspaceManager, event_bus: EventBus
    ) -> None:
        info = await manager.create(task_id="task-1", session_id="session-1")

        events = await event_bus.replay("session-1")
        payload = events[0].payload
        assert payload["path"] == info.path
