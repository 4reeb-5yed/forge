"""Tests for GitHub VCS commit functionality.

Verifies that GitHubVCS.commit() properly handles:
1. "nothing to commit" output on stdout (not stderr)
2. Genuine failures with non-empty error messages

Requirements: 10.1, 10.2, 10.3
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.adapters.github_vcs import GitHubVCS, NothingToCommitError


class TestGitHubVCSCommit:
    """Test GitHubVCS.commit() error handling."""

    @pytest.fixture
    def vcs(self) -> GitHubVCS:
        """Create a GitHubVCS instance with mocked token."""
        return GitHubVCS(token="test-token")

    @pytest.mark.asyncio
    async def test_commit_nothing_to_commit_on_stdout(self, vcs: GitHubVCS) -> None:
        """Verify 'nothing to commit' on stdout raises NothingToCommitError.
        
        This reproduces the original bug: git commit outputs "nothing to commit"
        to stdout, not stderr. The previous code only captured stderr, resulting
        in empty error messages.
        """
        # Mock _run_git to simulate git commit with "nothing to commit" on stdout
        async def mock_run_git(*args, **kwargs):
            if args[0] == "add":
                return (0, "", "")  # git add succeeds
            elif args[0] == "commit":
                # git outputs "nothing to commit, working tree clean" to STDOUT
                return (1, "nothing to commit, working tree clean\n", "")
            elif args[0] == "rev-parse":
                return (0, "", "")
            return (0, "", "")
        
        with patch.object(vcs, "_run_git", side_effect=mock_run_git):
            with pytest.raises(NothingToCommitError) as exc_info:
                await vcs.commit("/fake/path", "test message")
            
            # The error message should contain the actual git output
            assert "nothing to commit" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_commit_nothing_to_commit_on_stderr(self, vcs: GitHubVCS) -> None:
        """Verify 'nothing to commit' on stderr also raises NothingToCommitError."""
        async def mock_run_git(*args, **kwargs):
            if args[0] == "add":
                return (0, "", "")
            elif args[0] == "commit":
                # git could also output to stderr in some versions
                return (1, "", "nothing to commit, working tree clean\n")
            elif args[0] == "rev-parse":
                return (0, "", "")
            return (0, "", "")
        
        with patch.object(vcs, "_run_git", side_effect=mock_run_git):
            with pytest.raises(NothingToCommitError) as exc_info:
                await vcs.commit("/fake/path", "test message")
            
            assert "nothing to commit" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_commit_genuine_failure_includes_details(self, vcs: GitHubVCS) -> None:
        """Verify genuine git failures include stdout and stderr in message."""
        async def mock_run_git(*args, **kwargs):
            if args[0] == "add":
                return (0, "", "")
            elif args[0] == "commit":
                # Genuine failure with actual error messages
                return (1, "Some stdout error", "Permission denied")
            elif args[0] == "rev-parse":
                return (0, "", "")
            return (0, "", "")
        
        with patch.object(vcs, "_run_git", side_effect=mock_run_git):
            with pytest.raises(RuntimeError) as exc_info:
                await vcs.commit("/fake/path", "test message")
            
            error_msg = str(exc_info.value)
            # Should include the actual error details, not an empty message
            assert "git commit failed" in error_msg
            assert "Permission denied" in error_msg or "Some stdout error" in error_msg
            # Should NOT be just "git commit failed: " with nothing after

    @pytest.mark.asyncio
    async def test_commit_success(self, vcs: GitHubVCS) -> None:
        """Verify successful commit returns SHA."""
        async def mock_run_git(*args, **kwargs):
            if args[0] == "add":
                return (0, "", "")
            elif args[0] == "commit":
                return (0, "", "")
            elif args[0] == "rev-parse":
                return (0, "abc123def456\n", "")
            return (0, "", "")
        
        with patch.object(vcs, "_run_git", side_effect=mock_run_git):
            sha = await vcs.commit("/fake/path", "test message")
            assert sha == "abc123def456"

    @pytest.mark.asyncio
    async def test_add_failure(self, vcs: GitHubVCS) -> None:
        """Verify git add failure raises RuntimeError with stderr."""
        async def mock_run_git(*args, **kwargs):
            if args[0] == "add":
                return (1, "", "fatal: pathspec did not match any files")
            return (0, "", "")
        
        with patch.object(vcs, "_run_git", side_effect=mock_run_git):
            with pytest.raises(RuntimeError) as exc_info:
                await vcs.commit("/fake/path", "test message")
            
            assert "git add failed" in str(exc_info.value)


class TestNothingToCommitError:
    """Test NothingToCommitError exception."""

    def test_is_runtime_error(self) -> None:
        """Verify NothingToCommitError is a RuntimeError subclass."""
        assert issubclass(NothingToCommitError, RuntimeError)

    def test_can_be_caught_as_runtime_error(self) -> None:
        """Verify NothingToCommitError can be caught as RuntimeError."""
        with pytest.raises(RuntimeError):
            raise NothingToCommitError("test")

    def test_error_message_preserved(self) -> None:
        """Verify error message is preserved."""
        msg = "nothing to commit: working tree clean"
        error = NothingToCommitError(msg)
        assert str(error) == msg
