"""Sandboxed Aider Coding Tool — executes in an ephemeral Docker container.

Security improvements over the direct-subprocess AiderTool:
1. Workspace mounted read-write, but nothing else accessible
2. No access to host env vars (DB credentials, GitHub token, API keys)
3. Network disabled by default (--network none)
4. Resource limits (memory, CPU, PID)
5. Non-root user inside container
6. Read-only root filesystem with writable tmpfs for workspace only
7. Docker-level stop-timeout as backstop beyond asyncio timeout
8. Only the OPENROUTER_API_KEY is passed (Aider needs it for AI calls)

The existing ToolResult interface is preserved — this is a drop-in replacement.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.runtime.types import Health, ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # 5 minutes
DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_IMAGE = "forge-aider-sandbox:latest"
DEFAULT_MEMORY_LIMIT = "2g"
DEFAULT_CPU_LIMIT = "2.0"
DEFAULT_PID_LIMIT = 256


class SandboxedAiderTool:
    """Aider coding tool that executes in an isolated Docker container per task.

    Each execute() call:
    1. Launches an ephemeral container with the workspace volume-mounted
    2. Runs Aider inside the container with restricted permissions
    3. Captures output and destroys the container on completion
    4. Returns ToolResult (same interface as direct AiderTool)

    Security properties:
    - No host filesystem access beyond the workspace directory
    - No host network access (--network none)
    - No access to Forge API credentials (only OPENROUTER_API_KEY passed)
    - Resource-limited (memory, CPU, PIDs)
    - Non-root execution inside container
    - Read-only root filesystem
    """

    name: str = "aider-sandboxed"

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        image: str = DEFAULT_IMAGE,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        cpu_limit: str = DEFAULT_CPU_LIMIT,
        pid_limit: int = DEFAULT_PID_LIMIT,
        allow_network: bool = False,
        openrouter_api_key: str | None = None,
    ) -> None:
        """Initialize the sandboxed Aider tool.

        Args:
            model: AI model for Aider (default: claude-sonnet-4-20250514).
            timeout: Max execution time in seconds.
            image: Docker image with Aider pre-installed.
            memory_limit: Container memory limit (e.g., "2g").
            cpu_limit: Container CPU limit (e.g., "2.0").
            pid_limit: Max PIDs inside container.
            allow_network: If True, container gets network access. Default: False.
            openrouter_api_key: API key for AI calls. If None, reads from env.
        """
        self._model = model or os.environ.get("AIDER_MODEL", DEFAULT_MODEL)
        self._timeout = timeout
        self._image = image
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._pid_limit = pid_limit
        self._allow_network = allow_network
        self._openrouter_api_key = openrouter_api_key or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )

    async def execute(
        self, task_description: str, workspace_path: str
    ) -> ToolResult:
        """Execute a coding task in a sandboxed Docker container.

        The container:
        - Mounts workspace_path as /workspace (read-write)
        - Runs as non-root user (uid 1000)
        - Has no network access (unless allow_network=True)
        - Has resource limits (memory, CPU, PIDs)
        - Has a read-only root filesystem
        - Only receives OPENROUTER_API_KEY (for AI model calls)
        - Is killed and removed after timeout or completion

        After execution, captures the git diff produced by Aider for audit
        purposes. The diff is included in the ToolResult output for the
        audit trail to record.

        Args:
            task_description: Natural language description of the coding task.
            workspace_path: Host path to the workspace directory.

        Returns:
            ToolResult with success status, output (includes diff), and error.
        """
        container_name = f"forge-task-{os.urandom(4).hex()}"

        # Build docker run command with all security restrictions
        cmd = self._build_docker_command(
            container_name=container_name,
            workspace_path=workspace_path,
            task_description=task_description,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout + 10  # Grace period
                )
            except asyncio.TimeoutError:
                # Force-kill the container at Docker level
                await self._force_kill_container(container_name)
                logger.warning(
                    "Sandboxed Aider timed out after %ds for task in %s",
                    self._timeout,
                    workspace_path,
                )
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Sandboxed Aider timed out after {self._timeout}s",
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            success = proc.returncode == 0

            if not success:
                logger.warning(
                    "Sandboxed Aider exited with code %d for task in %s",
                    proc.returncode,
                    workspace_path,
                )

            # ─── Capture git diff for audit logging ───────────────────────
            diff_output = await self._capture_workspace_diff(workspace_path)
            if diff_output:
                # Prepend diff to output for audit trail visibility
                audit_section = (
                    "\n--- WORKSPACE DIFF (post-execution) ---\n"
                    f"{diff_output}\n"
                    "--- END DIFF ---\n"
                )
                stdout = stdout + audit_section

            return ToolResult(
                success=success,
                output=stdout,
                error=stderr if not success else "",
            )

        except FileNotFoundError:
            return ToolResult(
                success=False,
                output="",
                error="docker command not found. Is Docker installed?",
            )
        except Exception as exc:
            # Ensure container is cleaned up on any failure
            await self._force_kill_container(container_name)
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
            )

    def _build_docker_command(
        self,
        container_name: str,
        workspace_path: str,
        task_description: str,
    ) -> list[str]:
        """Build the full docker run command with security restrictions.

        Returns a list of strings suitable for subprocess exec.
        """
        cmd = [
            "docker", "run",
            # Container identity and lifecycle
            "--name", container_name,
            "--rm",  # Auto-remove on exit
            # Resource limits
            "--memory", self._memory_limit,
            "--cpus", self._cpu_limit,
            "--pids-limit", str(self._pid_limit),
            # Security: non-root user
            "--user", "1000:1000",
            # Security: read-only root filesystem
            "--read-only",
            # Security: writable tmpfs for temp files only
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
            # Security: drop all capabilities, add none back
            "--cap-drop", "ALL",
            # Security: no new privileges
            "--security-opt", "no-new-privileges",
            # Docker-level timeout as backstop
            "--stop-timeout", str(int(self._timeout)),
            # Workspace mount (the ONLY writable host path)
            "-v", f"{workspace_path}:/workspace:rw",
            "--workdir", "/workspace",
        ]

        # Network isolation (default: no network)
        if not self._allow_network:
            cmd.extend(["--network", "none"])

        # Environment: ONLY pass the AI API key (Aider needs it)
        # No GitHub token, no DB credentials, no other secrets
        if self._openrouter_api_key:
            cmd.extend(["-e", f"OPENROUTER_API_KEY={self._openrouter_api_key}"])

        # The image and command
        cmd.extend([
            self._image,
            "aider",
            "--yes",
            "--no-git",
            "--model", self._model,
            "--message", task_description,
        ])

        return cmd

    async def _force_kill_container(self, container_name: str) -> None:
        """Force-kill and remove a container. Best-effort, never raises."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            pass  # Best-effort cleanup

    async def _capture_workspace_diff(self, workspace_path: str) -> str:
        """Capture the git diff in the workspace after Aider execution.

        Returns the diff output (truncated to 50KB for audit storage limits),
        or empty string if git is unavailable or workspace has no changes.
        """
        MAX_DIFF_SIZE = 50_000  # 50KB cap to prevent audit bloat

        try:
            # First try staged changes
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--stat",
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stat_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            stat_output = stat_bytes.decode("utf-8", errors="replace").strip()

            if not stat_output:
                # Check untracked files
                proc = await asyncio.create_subprocess_exec(
                    "git", "ls-files", "--others", "--exclude-standard",
                    cwd=workspace_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                untracked_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                untracked = untracked_bytes.decode("utf-8", errors="replace").strip()
                if untracked:
                    return f"[Untracked files]\n{untracked}"
                return ""

            # Get the actual diff content
            proc = await asyncio.create_subprocess_exec(
                "git", "diff",
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            diff_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            diff_output = diff_bytes.decode("utf-8", errors="replace")

            # Truncate if too large
            if len(diff_output) > MAX_DIFF_SIZE:
                diff_output = diff_output[:MAX_DIFF_SIZE] + "\n... [truncated, full diff exceeds 50KB]"

            return diff_output

        except (FileNotFoundError, asyncio.TimeoutError, Exception) as exc:
            logger.debug("Failed to capture workspace diff: %s", exc)
            return ""

    async def health_check(self) -> Health:
        """Check that Docker is available and the sandbox image exists.

        Verifies:
        1. docker command is available
        2. The sandbox image is pulled/built
        """
        try:
            # Check docker is available
            proc = await asyncio.create_subprocess_exec(
                "docker", "version", "--format", "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=10.0
            )
            if proc.returncode != 0:
                return Health.unhealthy("docker not available or daemon not running")

            # Check image exists
            proc = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", self._image,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=10.0
            )
            if proc.returncode != 0:
                return Health.unhealthy(
                    f"Sandbox image '{self._image}' not found. "
                    f"Build it with: docker build -t {self._image} -f Dockerfile.sandbox ."
                )

            return Health.healthy()

        except FileNotFoundError:
            return Health.unhealthy("docker command not found")
        except asyncio.TimeoutError:
            return Health.unhealthy("docker health check timed out")
        except Exception as exc:
            return Health.unhealthy(str(exc))
