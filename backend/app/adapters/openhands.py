"""OpenHands Cloud Coding Tool adapter.

Uses the OpenHands Cloud API (https://app.all-hands.dev/api) to execute
coding tasks against a GitHub repository. OpenHands provides its own AI
model internally (free tier), handles sandboxed execution, and commits
results to the repo.

This replaces Aider as the coding tool when OPENHANDS_API_KEY is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.shared import Health, ToolResult

logger = logging.getLogger(__name__)

OPENHANDS_API_BASE = "https://app.all-hands.dev/api"
DEFAULT_TIMEOUT = 300  # 5 minutes for task completion
POLL_INTERVAL = 5  # seconds between status checks


class OpenHandsTool:
    """OpenHands Cloud coding tool.

    Creates a conversation on OpenHands Cloud with a repository and task
    description. OpenHands handles AI model selection, sandboxed execution,
    and code generation internally.

    The tool polls for completion and returns the result.
    """

    name: str = "openhands"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENHANDS_API_KEY", "")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "X-Session-API-Key": self._api_key,
            "Content-Type": "application/json",
        }

    async def execute(
        self, task_description: str, workspace_path: str, repo_url: str = ""
    ) -> ToolResult:
        """Execute a coding task via OpenHands Cloud.

        Creates a conversation with the task, waits for completion,
        and returns the result.

        Args:
            task_description: Natural language description of the coding task.
            workspace_path: Local workspace path (used for context, not execution).
            repo_url: GitHub repository URL for OpenHands to work on.

        Returns:
            ToolResult with success status and output.
        """
        if not self._api_key:
            return ToolResult(
                success=False,
                output="",
                error="OPENHANDS_API_KEY not configured",
            )

        # Extract repo identifier from URL (e.g., "forge-runtime/forge-e2e-test")
        repo_id = self._extract_repo_id(repo_url)

        try:
            # Create a new conversation
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{OPENHANDS_API_BASE}/conversations",
                    headers=self._headers(),
                    json={
                        "repository": repo_id,
                        "initial_user_msg": task_description,
                    },
                )

                if response.status_code != 200:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"OpenHands API error: {response.status_code} {response.text[:200]}",
                    )

                data = response.json()
                conversation_id = data.get("conversation_id", "")
                logger.info(
                    "OpenHands conversation created: %s", conversation_id
                )

            # Poll for completion
            result = await self._poll_for_completion(conversation_id)
            return result

        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=f"OpenHands request timed out after {self._timeout}s",
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"OpenHands error: {exc}",
            )

    async def _poll_for_completion(self, conversation_id: str) -> ToolResult:
        """Poll OpenHands for conversation completion.

        Checks status every POLL_INTERVAL seconds until done or timeout.
        """
        start = time.time()

        while (time.time() - start) < self._timeout:
            await asyncio.sleep(POLL_INTERVAL)

            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(
                        f"{OPENHANDS_API_BASE}/conversations/{conversation_id}",
                        headers=self._headers(),
                    )

                    if response.status_code != 200:
                        continue

                    data = response.json()
                    status = data.get("status", "")

                    if status == "STOPPED":
                        # Conversation finished
                        logger.info(
                            "OpenHands conversation %s completed", conversation_id
                        )
                        return ToolResult(
                            success=True,
                            output=f"OpenHands completed task (conversation: {conversation_id})",
                        )
                    elif status in ("STARTING", "RUNNING"):
                        continue
                    else:
                        # Unknown status — keep polling
                        continue

            except Exception as exc:
                logger.debug("Poll error for %s: %s", conversation_id, exc)
                continue

        # Timeout
        return ToolResult(
            success=False,
            output="",
            error=f"OpenHands conversation {conversation_id} timed out after {self._timeout}s",
        )

    def _extract_repo_id(self, url: str) -> str:
        """Extract 'owner/repo' from a GitHub URL.

        Examples:
            https://github.com/forge-runtime/forge-e2e-test.git → forge-runtime/forge-e2e-test
            https://github.com/forge-runtime/forge-e2e-test → forge-runtime/forge-e2e-test
        """
        if not url:
            return ""
        # Remove .git suffix and protocol
        url = url.rstrip("/").removesuffix(".git")
        parts = url.split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
        return url

    async def health_check(self) -> Health:
        """Check that OpenHands Cloud API is reachable."""
        if not self._api_key:
            return Health.unhealthy("OPENHANDS_API_KEY not set")

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try to list conversations as a health check
                response = await client.get(
                    f"{OPENHANDS_API_BASE}/conversations",
                    headers=self._headers(),
                    params={"limit": 1},
                )
                if response.status_code == 200:
                    return Health.healthy()
                return Health.unhealthy(f"OpenHands API returned {response.status_code}")
        except Exception as exc:
            return Health.unhealthy(str(exc))
