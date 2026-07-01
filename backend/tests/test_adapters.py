"""Tests for Forge adapters: OpenRouter, GitHub VCS, and Aider Tool."""

from __future__ import annotations
import os

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import httpx
import pytest

from app.adapters.openrouter import OpenRouterProvider
from app.adapters.github_vcs import GitHubVCS
from app.adapters.aider_tool import AiderTool
from app.shared import PermanentError, Health


# ===========================================================================
# OpenRouter Tests
# ===========================================================================


class TestOpenRouterComplete:
    """Tests for OpenRouterProvider.complete()"""

    @pytest.fixture
    def provider(self):
        return OpenRouterProvider(api_key="test-key")

    async def test_complete_success(self, provider):
        """Successful completion returns message content."""
        mock_response = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello, world!"}}]
            },
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_fn.return_value = mock_client

            result = await provider.complete(
                messages=[{"role": "user", "content": "Hi"}],
                model="openai/gpt-4",
            )

        assert result == "Hello, world!"

    async def test_complete_401_raises_permanent_error(self, provider):
        """401 response raises PermanentError."""
        mock_response = httpx.Response(
            401,
            text="Unauthorized",
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_fn.return_value = mock_client

            with pytest.raises(PermanentError, match="Authentication failed"):
                await provider.complete(
                    messages=[{"role": "user", "content": "Hi"}],
                    model="openai/gpt-4",
                )

    async def test_complete_403_raises_permanent_error(self, provider):
        """403 response raises PermanentError."""
        mock_response = httpx.Response(
            403,
            text="Forbidden",
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_fn.return_value = mock_client

            with pytest.raises(PermanentError):
                await provider.complete(
                    messages=[{"role": "user", "content": "Hi"}],
                    model="openai/gpt-4",
                )

    async def test_complete_429_raises_runtime_error(self, provider):
        """429 response raises RuntimeError with Retry-After info."""
        mock_response = httpx.Response(
            429,
            text="Too Many Requests",
            headers={"Retry-After": "30"},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_fn.return_value = mock_client

            with pytest.raises(RuntimeError, match="Rate limited.*Retry-After: 30"):
                await provider.complete(
                    messages=[{"role": "user", "content": "Hi"}],
                    model="openai/gpt-4",
                )

    async def test_complete_500_raises_runtime_error(self, provider):
        """5xx response raises RuntimeError (transient)."""
        mock_response = httpx.Response(
            500,
            text="Internal Server Error",
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_fn.return_value = mock_client

            with pytest.raises(RuntimeError, match="Server error"):
                await provider.complete(
                    messages=[{"role": "user", "content": "Hi"}],
                    model="openai/gpt-4",
                )


class TestOpenRouterStream:
    """Tests for OpenRouterProvider.stream()"""

    async def test_stream_yields_tokens(self):
        """Stream yields token chunks from SSE lines."""
        provider = OpenRouterProvider(api_key="test-key")

        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ]

        # Create a mock async stream context manager
        mock_response = AsyncMock()
        mock_response.status_code = 200

        async def mock_aiter_lines():
            for line in lines:
                yield line

        mock_response.aiter_lines = mock_aiter_lines

        mock_client = AsyncMock()
        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_cm.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = MagicMock(return_value=mock_stream_cm)

        with patch.object(provider, "_get_client", return_value=mock_client):
            tokens = []
            async for token in provider.stream(
                messages=[{"role": "user", "content": "Hi"}],
                model="openai/gpt-4",
            ):
                tokens.append(token)

        assert tokens == ["Hello", " world"]


class TestOpenRouterHealthCheck:
    """Tests for OpenRouterProvider.health_check()"""

    async def test_health_check_healthy(self):
        """200 from /models returns healthy."""
        provider = OpenRouterProvider(api_key="test-key")
        mock_response = httpx.Response(
            200,
            json={"data": []},
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        )

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_fn.return_value = mock_client

            health = await provider.health_check()

        assert health.ok is True

    async def test_health_check_unhealthy(self):
        """Non-200 from /models returns unhealthy."""
        provider = OpenRouterProvider(api_key="test-key")
        mock_response = httpx.Response(
            503,
            text="Service Unavailable",
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        )

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_fn.return_value = mock_client

            health = await provider.health_check()

        assert health.ok is False

    async def test_health_check_exception(self):
        """Exception returns unhealthy."""
        provider = OpenRouterProvider(api_key="test-key")

        with patch.object(provider, "_get_client") as mock_client_fn:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
            mock_client_fn.return_value = mock_client

            health = await provider.health_check()

        assert health.ok is False


class TestOpenRouterCallAdapter:
    """Tests for OpenRouterProvider.as_call_adapter()"""

    async def test_call_adapter_delegates_to_complete(self):
        """as_call_adapter() returns a callable that delegates to complete."""
        provider = OpenRouterProvider(api_key="test-key")

        with patch.object(provider, "complete", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "response"
            adapter = provider.as_call_adapter()
            result = await adapter("openrouter", "openai/gpt-4", [{"role": "user", "content": "Hi"}])

        assert result == "response"
        mock_complete.assert_called_once_with(
            messages=[{"role": "user", "content": "Hi"}],
            model="openai/gpt-4",
        )


# ===========================================================================
# GitHub VCS Tests
# ===========================================================================


class TestGitHubVCSClone:
    """Tests for GitHubVCS.clone()"""

    async def test_clone_success(self):
        """Successful clone calls git with token-injected URL."""
        vcs = GitHubVCS(token="ghp_testtoken123")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await vcs.clone(
                "https://github.com/owner/repo",
                "main",
                "/tmp/dest",
            )

        # Verify git clone was called with token in URL
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "git"
        assert call_args[1] == "clone"
        assert "--depth=1" in call_args
        assert "--branch" in call_args
        assert "main" in call_args
        # Token should be injected into URL
        url_arg = [a for a in call_args if "ghp_testtoken123@" in a]
        assert len(url_arg) == 1

    async def test_clone_failure_raises(self):
        """Failed clone raises RuntimeError without exposing token."""
        vcs = GitHubVCS(token="ghp_secret")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(
                return_value=(b"", b"fatal: auth ghp_secret failed")
            )
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError, match="git clone failed"):
                await vcs.clone("https://github.com/o/r", "main", "/tmp/d")


class TestGitHubVCSCommit:
    """Tests for GitHubVCS.commit()"""

    async def test_commit_returns_sha(self):
        """Successful commit returns the SHA."""
        vcs = GitHubVCS(token="ghp_test")
        sha = "abc123def456"

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            # git add -A
            add_proc = AsyncMock()
            add_proc.communicate = AsyncMock(return_value=(b"", b""))
            add_proc.returncode = 0

            # git commit -m
            commit_proc = AsyncMock()
            commit_proc.communicate = AsyncMock(return_value=(b"", b""))
            commit_proc.returncode = 0

            # git rev-parse HEAD
            rev_proc = AsyncMock()
            rev_proc.communicate = AsyncMock(return_value=(f"{sha}\n".encode(), b""))
            rev_proc.returncode = 0

            mock_exec.side_effect = [add_proc, commit_proc, rev_proc]

            result = await vcs.commit("/workspace", "fix: something")

        assert result == sha

    async def test_commit_add_failure_raises(self):
        """If git add fails, raises RuntimeError."""
        vcs = GitHubVCS(token="ghp_test")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            add_proc = AsyncMock()
            add_proc.communicate = AsyncMock(return_value=(b"", b"error"))
            add_proc.returncode = 1
            mock_exec.return_value = add_proc

            with pytest.raises(RuntimeError, match="git add failed"):
                await vcs.commit("/workspace", "msg")


class TestGitHubVCSPush:
    """Tests for GitHubVCS.push()"""

    async def test_push_success(self):
        """Successful push completes without error."""
        vcs = GitHubVCS(token="ghp_test")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await vcs.push("/workspace")  # Should not raise

    async def test_push_failure_raises(self):
        """Failed push raises RuntimeError."""
        vcs = GitHubVCS(token="ghp_test")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"rejected"))
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError, match="git push failed"):
                await vcs.push("/workspace")


class TestGitHubVCSHealthCheck:
    """Tests for GitHubVCS.health_check()"""

    async def test_health_check_no_token(self):
        """Missing token returns unhealthy."""
        original_env = os.environ.get("GITHUB_TOKEN")
        try:
            if "GITHUB_TOKEN" in os.environ:
                del os.environ["GITHUB_TOKEN"]
            vcs = GitHubVCS()
            health = await vcs.health_check()
            assert health.ok is False
            assert "GITHUB_TOKEN" in health.message
        finally:
            if original_env is not None:
                os.environ["GITHUB_TOKEN"] = original_env

    async def test_health_check_healthy(self):
        """With token and working git, returns healthy."""
        vcs = GitHubVCS(token="ghp_test")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"git version 2.40.0\n", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            health = await vcs.health_check()

        assert health.ok is True


# ===========================================================================
# Aider Tool Tests
# ===========================================================================


class TestAiderToolExecute:
    """Tests for AiderTool.execute()"""

    async def test_execute_success(self):
        """Successful execution returns ToolResult with success=True."""
        tool = AiderTool(model="gpt-4", timeout=60)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(
                return_value=(b"Applied changes to file.py\n", b"")
            )
            mock_proc.returncode = 0
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc

            result = await tool.execute("Add a docstring", "/workspace")

        assert result.success is True
        assert "Applied changes" in result.output
        assert result.error == ""

    async def test_execute_failure(self):
        """Non-zero exit returns ToolResult with success=False."""
        tool = AiderTool(model="gpt-4", timeout=60)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(
                return_value=(b"", b"Error: model unavailable")
            )
            mock_proc.returncode = 1
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc

            result = await tool.execute("Add a docstring", "/workspace")

        assert result.success is False
        assert "model unavailable" in result.error

    async def test_execute_timeout(self):
        """Timeout kills process and returns failure."""
        tool = AiderTool(model="gpt-4", timeout=1)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc

            result = await tool.execute("Long task", "/workspace")

        assert result.success is False
        assert "timed out" in result.error
        mock_proc.kill.assert_called_once()

    async def test_execute_command_not_found(self):
        """Missing aider binary returns appropriate error."""
        tool = AiderTool(model="gpt-4", timeout=60)

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError(),
        ):
            result = await tool.execute("task", "/workspace")

        assert result.success is False
        assert "not found" in result.error


class TestAiderToolHealthCheck:
    """Tests for AiderTool.health_check()"""

    async def test_health_check_healthy(self):
        """Working aider --version returns healthy."""
        tool = AiderTool(model="gpt-4")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"aider 0.40.0\n", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            health = await tool.health_check()

        assert health.ok is True

    async def test_health_check_not_installed(self):
        """Missing aider returns unhealthy."""
        tool = AiderTool(model="gpt-4")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError(),
        ):
            health = await tool.health_check()

        assert health.ok is False
        assert "not found" in health.message

    async def test_health_check_timeout(self):
        """Timeout on aider --version returns unhealthy."""
        tool = AiderTool(model="gpt-4")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_exec.return_value = mock_proc

            health = await tool.health_check()

        assert health.ok is False
        assert "timed out" in health.message
