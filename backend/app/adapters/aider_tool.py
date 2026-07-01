"""Aider Coding Tool adapter.

Spawns Aider as a subprocess to execute coding tasks in a workspace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from app.shared import Health, ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # 5 minutes
DEFAULT_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"


class AiderTool:
    """Aider coding tool that spawns aider as a subprocess.

    Reads AIDER_MODEL from environment (default: claude-sonnet-4-20250514).
    Configurable timeout with process kill on expiry.
    """

    name: str = "aider"

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._model = model or os.environ.get("AIDER_MODEL", DEFAULT_MODEL)
        self._timeout = timeout

    async def execute(
        self, task_description: str, workspace_path: str
    ) -> ToolResult:
        """Execute a coding task using Aider.

        Spawns `aider --yes --no-git --model {model} --message {task_description}`
        in the workspace_path as cwd.

        Args:
            task_description: Natural language description of the coding task.
            workspace_path: Working directory for Aider to operate in.

        Returns:
            ToolResult with success status, output, and error.
        """
        cmd = [
            "aider",
            "--yes",
            "--no-git",
            "--model", self._model,
            "--message", task_description,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                # Kill the process on timeout
                proc.kill()
                await proc.wait()
                logger.warning(
                    "Aider timed out after %ds for task in %s",
                    self._timeout,
                    workspace_path,
                )
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Aider timed out after {self._timeout}s",
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            success = proc.returncode == 0

            if not success:
                logger.warning(
                    "Aider exited with code %d for task in %s",
                    proc.returncode,
                    workspace_path,
                )

            return ToolResult(
                success=success,
                output=stdout,
                error=stderr if not success else "",
            )

        except FileNotFoundError:
            return ToolResult(
                success=False,
                output="",
                error="aider command not found. Is aider installed?",
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
            )

    async def health_check(self) -> Health:
        """Check that aider is installed and runnable.

        Runs `aider --version` and checks the exit code.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "aider", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=10.0
            )
            if proc.returncode == 0:
                version = stdout_bytes.decode("utf-8", errors="replace").strip()
                return Health.healthy()
            return Health.unhealthy(f"aider --version exited with code {proc.returncode}")
        except FileNotFoundError:
            return Health.unhealthy("aider command not found")
        except asyncio.TimeoutError:
            return Health.unhealthy("aider --version timed out")
        except Exception as exc:
            return Health.unhealthy(str(exc))
