"""Diff-scope verification — blocks commits that touch paths outside the task's declared scope.

This is a pre-commit check that diffs the workspace against the base ref and
flags/blocks changes to sensitive or out-of-scope paths.

Security purpose: catches both AI bugs and prompt-injected instructions that
try to modify workflow files, credentials, or unrelated code.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths that are ALWAYS blocked from modification by AI-generated code
BLOCKED_PATHS = frozenset({
    ".github/workflows/",
    ".github/actions/",
    ".env",
    ".env.local",
    ".env.production",
    "secrets/",
    ".ssh/",
    "Dockerfile",
    "docker-compose.yml",
    ".kiro/",
})

# File patterns that are suspicious (flagged but not necessarily blocked)
SUSPICIOUS_PATTERNS = frozenset({
    "*.pem",
    "*.key",
    "*.cert",
    "id_rsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
})


@dataclass
class ScopeCheckResult:
    """Result of a diff-scope check."""

    passed: bool
    blocked_files: list[str] = field(default_factory=list)
    out_of_scope_files: list[str] = field(default_factory=list)
    reason: str = ""


async def check_diff_scope(
    workspace_path: str,
    allowed_paths: list[str] | None = None,
) -> ScopeCheckResult:
    """Check that workspace changes only touch allowed paths.

    Args:
        workspace_path: Path to the git workspace.
        allowed_paths: If provided, only changes within these paths are allowed.
            If None, all paths are allowed EXCEPT blocked ones.

    Returns:
        ScopeCheckResult indicating pass/fail with details.
    """
    # Get list of changed files via git diff
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "--cached",
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await proc.communicate()
        if proc.returncode != 0:
            # Try unstaged diff instead
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--name-only",
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await proc.communicate()

        # Also check untracked files
        proc2 = await asyncio.create_subprocess_exec(
            "git", "ls-files", "--others", "--exclude-standard",
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        untracked_bytes, _ = await proc2.communicate()

        changed_files = stdout_bytes.decode("utf-8", errors="replace").strip().split("\n")
        untracked_files = untracked_bytes.decode("utf-8", errors="replace").strip().split("\n")

        all_changed = [f for f in changed_files + untracked_files if f.strip()]

    except FileNotFoundError:
        return ScopeCheckResult(
            passed=True,
            reason="git not available — scope check skipped",
        )
    except Exception as exc:
        logger.warning("Scope check failed: %s", exc)
        return ScopeCheckResult(
            passed=True,
            reason=f"Scope check error: {exc} — allowing proceed",
        )

    if not all_changed:
        return ScopeCheckResult(passed=True)

    # Check for blocked paths
    blocked: list[str] = []
    out_of_scope: list[str] = []

    for filepath in all_changed:
        # Check against always-blocked paths
        for blocked_path in BLOCKED_PATHS:
            if filepath.startswith(blocked_path) or filepath == blocked_path.rstrip("/"):
                blocked.append(filepath)
                break

        # Check against suspicious patterns
        for pattern in SUSPICIOUS_PATTERNS:
            if pattern.startswith("*"):
                if filepath.endswith(pattern[1:]):
                    blocked.append(filepath)
                    break
            elif filepath.endswith(pattern):
                blocked.append(filepath)
                break

        # If allowed_paths specified, check scope
        if allowed_paths is not None:
            in_scope = any(
                filepath.startswith(prefix) or filepath == prefix
                for prefix in allowed_paths
            )
            if not in_scope and filepath not in blocked:
                out_of_scope.append(filepath)

    if blocked:
        return ScopeCheckResult(
            passed=False,
            blocked_files=blocked,
            reason=f"Changes touch {len(blocked)} blocked path(s): {', '.join(blocked[:5])}",
        )

    if out_of_scope:
        return ScopeCheckResult(
            passed=False,
            out_of_scope_files=out_of_scope,
            reason=f"Changes touch {len(out_of_scope)} path(s) outside task scope: {', '.join(out_of_scope[:5])}",
        )

    return ScopeCheckResult(passed=True)
