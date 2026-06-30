"""GitHub VCS Adapter.

Implements VCS operations (clone, commit, push) using git subprocess calls.
Injects GITHUB_TOKEN into clone URLs for authentication.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from urllib.parse import urlparse, urlunparse

from app.runtime.types import Health

logger = logging.getLogger(__name__)


class GitHubVCS:
    """GitHub VCS connector using asyncio subprocess for git operations.

    Reads GITHUB_TOKEN from environment. Never logs the token.
    """

    name: str = "github"

    def __init__(self, *, token: str | None = None) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "")

    def _inject_token(self, url: str) -> str:
        """Inject the GitHub token into a clone URL.

        Transforms https://github.com/owner/repo into
        https://{token}@github.com/owner/repo
        """
        parsed = urlparse(url)
        authed = parsed._replace(netloc=f"{self._token}@{parsed.hostname}")
        return urlunparse(authed)

    async def _run_git(
        self, *args: str, cwd: str | None = None
    ) -> tuple[int, str, str]:
        """Run a git command and return (returncode, stdout, stderr).

        Never logs arguments that might contain the token.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return proc.returncode or 0, stdout, stderr

    async def clone(self, url: str, ref: str, dest_path: str) -> None:
        """Clone a repository at a specific ref.

        Args:
            url: The repository URL (e.g. https://github.com/owner/repo).
            ref: Branch or tag to clone.
            dest_path: Local destination path.

        Raises:
            RuntimeError: If the clone fails.
        """
        authed_url = self._inject_token(url)
        returncode, stdout, stderr = await self._run_git(
            "clone", "--depth=1", "--branch", ref, authed_url, dest_path
        )
        if returncode != 0:
            # Sanitize output to never expose token
            safe_stderr = self._sanitize(stderr)
            raise RuntimeError(f"git clone failed (exit {returncode}): {safe_stderr}")
        logger.info("Cloned %s (ref=%s) to %s", url, ref, dest_path)

    async def commit(self, workspace_path: str, message: str) -> str:
        """Stage all changes and commit.

        Args:
            workspace_path: Path to the git working directory.
            message: Commit message.

        Returns:
            The commit SHA.

        Raises:
            RuntimeError: If git add or git commit fails.
        """
        # Stage all changes
        rc, _, stderr = await self._run_git("add", "-A", cwd=workspace_path)
        if rc != 0:
            raise RuntimeError(f"git add failed: {stderr}")

        # Commit
        rc, _, stderr = await self._run_git(
            "commit", "-m", message, cwd=workspace_path
        )
        if rc != 0:
            raise RuntimeError(f"git commit failed: {stderr}")

        # Get commit SHA
        rc, stdout, stderr = await self._run_git(
            "rev-parse", "HEAD", cwd=workspace_path
        )
        if rc != 0:
            raise RuntimeError(f"git rev-parse failed: {stderr}")

        sha = stdout.strip()
        logger.info("Committed %s in %s", sha[:8], workspace_path)
        return sha

    async def push(self, workspace_path: str) -> None:
        """Push commits to the remote.

        Args:
            workspace_path: Path to the git working directory.

        Raises:
            RuntimeError: If git push fails.
        """
        rc, _, stderr = await self._run_git("push", cwd=workspace_path)
        if rc != 0:
            safe_stderr = self._sanitize(stderr)
            raise RuntimeError(f"git push failed: {safe_stderr}")
        logger.info("Pushed changes from %s", workspace_path)

    async def health_check(self) -> Health:
        """Check that GITHUB_TOKEN is set and git is available.

        Returns:
            Health status.
        """
        if not self._token:
            return Health.unhealthy("GITHUB_TOKEN not set")

        try:
            rc, stdout, _ = await self._run_git("--version")
            if rc == 0:
                return Health.healthy()
            return Health.unhealthy("git command failed")
        except Exception as exc:
            return Health.unhealthy(str(exc))

    def _sanitize(self, text: str) -> str:
        """Remove any token occurrences from text."""
        if self._token:
            return text.replace(self._token, "***")
        return text
