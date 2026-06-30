"""Tests for scope_check.py — diff-scope verification for commits.

Tests cover:
- Blocked paths detection (workflows, env files, secrets, Docker configs)
- Suspicious file pattern detection (keys, certs)
- Allowed-paths scoping (task-scoped whitelist)
- Git unavailable graceful fallback
- Empty workspace (no changes) passes
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.verification.scope_check import (
    BLOCKED_PATHS,
    SUSPICIOUS_PATTERNS,
    ScopeCheckResult,
    check_diff_scope,
)


def _mock_git_output(changed_files: str, untracked_files: str = ""):
    """Helper to mock git subprocess calls for scope check."""
    call_count = 0

    async def mock_subprocess(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_proc = AsyncMock()
        mock_proc.returncode = 0

        if call_count == 1:
            # git diff --name-only --cached
            mock_proc.communicate = AsyncMock(
                return_value=(changed_files.encode(), b"")
            )
        elif call_count == 2:
            # git ls-files --others --exclude-standard
            mock_proc.communicate = AsyncMock(
                return_value=(untracked_files.encode(), b"")
            )
        else:
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        return mock_proc

    return mock_subprocess


class TestBlockedPaths:
    """Verify that changes to always-blocked paths are rejected."""

    @pytest.mark.asyncio
    async def test_blocks_github_workflow_changes(self):
        mock = _mock_git_output(".github/workflows/deploy.yml\nsrc/main.py")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False
        assert ".github/workflows/deploy.yml" in result.blocked_files

    @pytest.mark.asyncio
    async def test_blocks_env_file_changes(self):
        mock = _mock_git_output(".env\nsrc/app.py")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False
        assert ".env" in result.blocked_files

    @pytest.mark.asyncio
    async def test_blocks_env_production_changes(self):
        mock = _mock_git_output(".env.production")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_blocks_secrets_directory(self):
        mock = _mock_git_output("secrets/api_key.txt")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False
        assert "secrets/api_key.txt" in result.blocked_files

    @pytest.mark.asyncio
    async def test_blocks_ssh_directory(self):
        mock = _mock_git_output(".ssh/id_rsa")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_blocks_dockerfile_changes(self):
        mock = _mock_git_output("Dockerfile")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_blocks_docker_compose_changes(self):
        mock = _mock_git_output("docker-compose.yml")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_blocks_kiro_directory(self):
        mock = _mock_git_output(".kiro/specs/feature/tasks.md")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False


class TestSuspiciousPatterns:
    """Verify that suspicious file patterns are blocked."""

    @pytest.mark.asyncio
    async def test_blocks_pem_files(self):
        mock = _mock_git_output("certs/server.pem")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_blocks_key_files(self):
        mock = _mock_git_output("tls/private.key")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_blocks_npmrc(self):
        mock = _mock_git_output(".npmrc")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_blocks_pypirc(self):
        mock = _mock_git_output(".pypirc")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is False


class TestAllowedPathsScoping:
    """Verify task-scoped path whitelist enforcement."""

    @pytest.mark.asyncio
    async def test_passes_when_changes_within_scope(self):
        mock = _mock_git_output("src/utils/helper.py\nsrc/utils/test.py")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope(
                "/tmp/ws", allowed_paths=["src/utils/"]
            )

        assert result.passed is True

    @pytest.mark.asyncio
    async def test_fails_when_changes_outside_scope(self):
        mock = _mock_git_output("src/utils/helper.py\nlib/other.py")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope(
                "/tmp/ws", allowed_paths=["src/utils/"]
            )

        assert result.passed is False
        assert "lib/other.py" in result.out_of_scope_files

    @pytest.mark.asyncio
    async def test_no_allowed_paths_permits_non_blocked(self):
        """Without allowed_paths, any non-blocked path is permitted."""
        mock = _mock_git_output("src/main.py\nlib/utils.py\ntests/test_all.py")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is True


class TestGracefulFallback:
    """Verify graceful behavior when git is unavailable."""

    @pytest.mark.asyncio
    async def test_passes_when_git_not_found(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("git not found"),
        ):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is True
        assert "git not available" in result.reason

    @pytest.mark.asyncio
    async def test_passes_on_unexpected_error(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=RuntimeError("unexpected"),
        ):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is True
        assert "error" in result.reason.lower()


class TestEmptyWorkspace:
    """Verify that workspaces with no changes pass."""

    @pytest.mark.asyncio
    async def test_no_changes_passes(self):
        mock = _mock_git_output("", "")
        with patch("asyncio.create_subprocess_exec", side_effect=mock):
            result = await check_diff_scope("/tmp/ws")

        assert result.passed is True


class TestWorkspaceLimitExceeded:
    """Test workspace limit enforcement in WorkspaceManager.create()."""

    @pytest.mark.asyncio
    async def test_raises_when_limit_reached(self):
        from app.runtime.events.bus import EventBus
        from app.runtime.workspace import (
            WorkspaceLimitExceededError,
            WorkspaceManager,
        )

        event_bus = EventBus()
        mgr = WorkspaceManager(event_bus=event_bus)

        # Fill up to limit
        for i in range(10):
            await mgr.create(
                task_id=f"task-{i:04d}",
                session_id="sess-1",
                max_concurrent=10,
            )

        # 11th should raise
        with pytest.raises(WorkspaceLimitExceededError) as exc_info:
            await mgr.create(
                task_id="task-overflow",
                session_id="sess-1",
                max_concurrent=10,
            )

        assert exc_info.value.current == 10
        assert exc_info.value.limit == 10

    @pytest.mark.asyncio
    async def test_allows_after_destroy(self):
        from app.runtime.events.bus import EventBus
        from app.runtime.workspace import WorkspaceManager

        event_bus = EventBus()
        mgr = WorkspaceManager(event_bus=event_bus)

        # Fill to limit
        workspaces = []
        for i in range(3):
            ws = await mgr.create(
                task_id=f"task-{i:04d}",
                session_id="sess-1",
                max_concurrent=3,
            )
            workspaces.append(ws)

        # Destroy one
        await mgr.destroy(workspaces[0].workspace_id)

        # Should now succeed
        ws = await mgr.create(
            task_id="task-new",
            session_id="sess-1",
            max_concurrent=3,
        )
        assert ws.workspace_id is not None
