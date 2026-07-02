"""Tests for SandboxedAiderTool — Docker-based sandbox execution.

Tests cover:
- Docker command construction with all security flags
- Timeout handling and container cleanup
- Diff capture after execution
- Health check logic
- Error paths (Docker not found, container failure)
- Default model configuration (must be OpenRouter-routed)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.sandboxed_aider import DEFAULT_MODEL, SandboxedAiderTool


@pytest.fixture
def tool():
    """Create a SandboxedAiderTool with test configuration."""
    return SandboxedAiderTool(
        model="test-model",
        timeout=60,
        image="forge-aider-sandbox:test",
        memory_limit="1g",
        cpu_limit="1.0",
        pid_limit=128,
        openrouter_api_key="test-key-123",
    )


class TestDockerCommandConstruction:
    """Verify the docker run command includes all security restrictions."""

    def test_command_includes_rm_flag(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        assert "--rm" in cmd

    def test_command_includes_memory_limit(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--memory")
        assert cmd[idx + 1] == "1g"

    def test_command_includes_cpu_limit(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--cpus")
        assert cmd[idx + 1] == "1.0"

    def test_command_includes_pid_limit(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--pids-limit")
        assert cmd[idx + 1] == "128"

    def test_command_runs_as_non_root(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--user")
        assert cmd[idx + 1] == "1000:1000"

    def test_command_has_read_only_root(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        assert "--read-only" in cmd

    def test_command_drops_all_capabilities(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--cap-drop")
        assert cmd[idx + 1] == "ALL"

    def test_command_no_new_privileges(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--security-opt")
        assert cmd[idx + 1] == "no-new-privileges"

    def test_command_has_network_none_by_default(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--network")
        assert cmd[idx + 1] == "none"

    def test_command_allows_network_when_configured(self):
        tool = SandboxedAiderTool(allow_network=True, openrouter_api_key="key")
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        assert "--network" not in cmd

    def test_command_mounts_workspace_rw(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        assert "-v" in cmd
        idx = cmd.index("-v")
        assert cmd[idx + 1] == "/tmp/workspace:/workspace:rw"

    def test_command_passes_only_openrouter_key(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        # Find all -e flags (environment variables)
        env_vars = []
        for i, arg in enumerate(cmd):
            if arg == "-e" and i + 1 < len(cmd):
                env_vars.append(cmd[i + 1])

        # Only OPENROUTER_API_KEY and HOME should be passed
        # HOME is required for Aider's cache directory inside the sandbox
        assert len(env_vars) == 2
        env_dict = {v.split("=")[0] for v in env_vars}
        assert env_dict == {"OPENROUTER_API_KEY", "HOME"}
        assert any(v.startswith("OPENROUTER_API_KEY=") for v in env_vars)
        assert any(v == "HOME=/home/sandbox" for v in env_vars)

    def test_command_does_not_pass_github_token(self, tool):
        """Security: GITHUB_TOKEN must never be passed to the sandbox."""
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        cmd_str = " ".join(cmd)
        assert "GITHUB_TOKEN" not in cmd_str
        assert "DATABASE_URL" not in cmd_str

    def test_command_uses_correct_image(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        assert "forge-aider-sandbox:test" in cmd

    def test_command_passes_task_via_message_flag(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="implement feature X",
        )
        idx = cmd.index("--message")
        assert cmd[idx + 1] == "implement feature X"

    def test_command_includes_tmpfs_for_tmp(self, tool):
        cmd = tool._build_docker_command(
            container_name="test-container",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--tmpfs")
        assert "noexec" in cmd[idx + 1]


class TestExecute:
    """Test the execute method with mocked subprocess."""

    @pytest.mark.asyncio
    async def test_successful_execution(self, tool):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Done!", b""))
        mock_proc.returncode = 0

        # Mock git diff capture (empty diff)
        mock_diff_proc = AsyncMock()
        mock_diff_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_diff_proc.returncode = 0

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_proc
            return mock_diff_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            result = await tool.execute("fix the bug", "/tmp/ws")

        assert result.success is True
        assert "Done!" in result.output

    @pytest.mark.asyncio
    async def test_failed_execution(self, tool):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Error occurred"))
        mock_proc.returncode = 1

        mock_diff_proc = AsyncMock()
        mock_diff_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_diff_proc.returncode = 0

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_proc
            return mock_diff_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            result = await tool.execute("fix the bug", "/tmp/ws")

        assert result.success is False
        assert "Error occurred" in result.error

    @pytest.mark.asyncio
    async def test_timeout_kills_container(self, tool):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.returncode = None

        mock_kill_proc = AsyncMock()
        mock_kill_proc.wait = AsyncMock(return_value=0)

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_proc
            return mock_kill_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            with patch("asyncio.wait_for", side_effect=[asyncio.TimeoutError(), None]):
                result = await tool.execute("fix the bug", "/tmp/ws")

        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_docker_not_found(self, tool):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("docker not found"),
        ):
            result = await tool.execute("fix the bug", "/tmp/ws")

        assert result.success is False
        assert "docker" in result.error.lower()

    @pytest.mark.asyncio
    async def test_diff_captured_in_output(self, tool):
        """After execution, git diff should be captured and appended to output."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Aider done", b""))
        mock_proc.returncode = 0

        # First call: docker run. Second call: git diff --stat. Third call: git diff.
        mock_stat_proc = AsyncMock()
        mock_stat_proc.communicate = AsyncMock(return_value=(b" file.py | 2 +-\n", b""))
        mock_stat_proc.returncode = 0

        mock_diff_proc = AsyncMock()
        mock_diff_proc.communicate = AsyncMock(
            return_value=(b"+added line\n-removed line\n", b"")
        )
        mock_diff_proc.returncode = 0

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_proc
            elif call_count == 2:
                return mock_stat_proc
            return mock_diff_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            result = await tool.execute("fix the bug", "/tmp/ws")

        assert result.success is True
        assert "WORKSPACE DIFF" in result.output
        assert "+added line" in result.output


class TestHealthCheck:
    """Test health check detects Docker and image availability."""

    @pytest.mark.asyncio
    async def test_healthy_when_docker_and_image_available(self, tool):
        mock_version_proc = AsyncMock()
        mock_version_proc.communicate = AsyncMock(return_value=(b"24.0.0", b""))
        mock_version_proc.returncode = 0

        mock_inspect_proc = AsyncMock()
        mock_inspect_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_inspect_proc.returncode = 0

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_version_proc
            return mock_inspect_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            health = await tool.health_check()

        # Health.healthy() returns HealthStatus.OK
        from app.runtime.types import HealthStatus
        assert health.status == HealthStatus.OK

    @pytest.mark.asyncio
    async def test_unhealthy_when_docker_not_found(self, tool):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError(),
        ):
            health = await tool.health_check()

        from app.runtime.types import HealthStatus
        assert health.status == HealthStatus.UNHEALTHY
        assert "not found" in health.message

    @pytest.mark.asyncio
    async def test_unhealthy_when_image_missing(self, tool):
        mock_version_proc = AsyncMock()
        mock_version_proc.communicate = AsyncMock(return_value=(b"24.0.0", b""))
        mock_version_proc.returncode = 0

        mock_inspect_proc = AsyncMock()
        mock_inspect_proc.communicate = AsyncMock(return_value=(b"", b"not found"))
        mock_inspect_proc.returncode = 1

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_version_proc
            return mock_inspect_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            health = await tool.health_check()

        from app.runtime.types import HealthStatus
        assert health.status == HealthStatus.UNHEALTHY
        assert "not found" in health.message.lower()


class TestDefaultModelConfiguration:
    """Verify the default model is OpenRouter-routed, not a raw provider model.

    The sandbox only ever receives OPENROUTER_API_KEY (never ANTHROPIC_API_KEY or
    any other provider key). A raw provider model name makes Aider try to call
    that provider directly, which then fails with a silent litellm.AuthenticationError
    — Aider exits 0 but produces zero file changes.
    """

    def test_default_model_is_openrouter_routed(self):
        """DEFAULT_MODEL must use the openrouter/<provider>/<model> format."""
        assert DEFAULT_MODEL.startswith("openrouter/"), (
            f"DEFAULT_MODEL must be OpenRouter-routed (openrouter/<provider>/<model>), "
            f"got: {DEFAULT_MODEL!r}. Raw provider model names cause Aider to call "
            f"the provider directly with OPENROUTER_API_KEY, which fails silently."
        )

    def test_default_model_is_not_bare_anthropic(self):
        """Bare Anthropic model names (no openrouter/ prefix) are wrong for the sandbox."""
        assert not DEFAULT_MODEL.startswith("claude-"), (
            f"DEFAULT_MODEL must not be a bare Anthropic model name like {DEFAULT_MODEL!r}. "
            f"The sandbox only has OPENROUTER_API_KEY, not ANTHROPIC_API_KEY."
        )
        assert "anthropic/" not in DEFAULT_MODEL or DEFAULT_MODEL.startswith("openrouter/"), (
            f"OpenRouter model names must start with 'openrouter/', got: {DEFAULT_MODEL!r}"
        )

    def test_tool_uses_openrouter_model_by_default(self):
        """A default-constructed tool passes an OpenRouter-routed model to Aider."""
        tool = SandboxedAiderTool(openrouter_api_key="test-key")
        assert tool._model.startswith("openrouter/"), (
            f"Tool default model must be OpenRouter-routed, got: {tool._model!r}"
        )

    def test_tool_passes_openrouter_model_to_aider(self):
        """The model passed to Aider inside the container must be OpenRouter-routed."""
        tool = SandboxedAiderTool(openrouter_api_key="test-key")
        cmd = tool._build_docker_command(
            container_name="test",
            workspace_path="/tmp/workspace",
            task_description="fix bug",
        )
        idx = cmd.index("--model")
        model_passed = cmd[idx + 1]
        assert model_passed.startswith("openrouter/"), (
            f"Model passed to Aider must be OpenRouter-routed, got: {model_passed!r}"
        )
