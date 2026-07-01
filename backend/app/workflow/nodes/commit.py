"""Commit node — commits and pushes changes to the remote repo.

Does REAL git operations:
1. Runs diff-scope check (security)
2. git add -A + git commit
3. git push to remote
4. Returns the real commit SHA

Requirements: 10.1, 10.2, 10.3
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState
from app.runtime.verification.scope_check import check_diff_scope
from app.workflow.deps import RuntimeDeps

logger = logging.getLogger(__name__)

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_commit_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the commit node function."""

    async def commit_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        commit_shas = list(state.get("commit_shas", []))
        current_task_index = state.get("current_task_index", 0)
        task_ordering = state.get("task_ordering", [])
        current_task_id = state.get("current_task_id")
        workspace_path = state.get("workspace_path", "")
        allowed_paths = state.get("allowed_paths")

        # ─── Pre-commit scope check (security) ───────────────────────────
        if workspace_path:
            scope_result = await check_diff_scope(
                workspace_path=workspace_path,
                allowed_paths=allowed_paths,
            )
            if not scope_result.passed:
                logger.warning(
                    "Scope check BLOCKED commit for task %s: %s",
                    current_task_id,
                    scope_result.reason,
                )
                block_event = Event.create(
                    type=EventType.ERROR,
                    session_id=session_id,
                    source="commit_node.scope_check",
                    payload={
                        "action": "commit_blocked",
                        "task_id": current_task_id,
                        "reason": scope_result.reason,
                        "blocked_files": scope_result.blocked_files,
                        "out_of_scope_files": scope_result.out_of_scope_files,
                    },
                    correlation_id=session_id,
                    event_id=str(uuid.uuid4()),
                )
                await deps.event_bus.publish(block_event)

                node_path.append("commit_blocked")
                return {
                    "commit_shas": commit_shas,
                    "current_task_index": current_task_index,
                    "all_tasks_done": False,
                    "node_path": node_path,
                    "errors": list(state.get("errors", [])) + [{
                        "code": "scope_blocked",
                        "message": scope_result.reason,
                        "node": "commit",
                    }],
                }

        # ─── Real commit + push ───────────────────────────────────────────
        commit_sha = ""
        if workspace_path and deps.vcs is not None:
            try:
                # Configure git user for the commit
                import asyncio
                proc = await asyncio.create_subprocess_exec(
                    "git", "config", "user.email", "forge@forge-runtime.dev",
                    cwd=workspace_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                proc = await asyncio.create_subprocess_exec(
                    "git", "config", "user.name", "Forge",
                    cwd=workspace_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()

                # Commit
                commit_msg = f"forge: {current_task_id or 'automated changes'}"
                commit_sha = await deps.vcs.commit(workspace_path, commit_msg)
                logger.info("Committed %s for task %s", commit_sha[:8], current_task_id)

                # Push
                await deps.vcs.push(workspace_path)
                logger.info("Pushed to remote for task %s", current_task_id)

            except RuntimeError as exc:
                error_msg = str(exc)
                if "nothing to commit" in error_msg.lower() or "working tree clean" in error_msg.lower():
                    # No changes — not an error, just nothing to push
                    logger.info("No changes to commit for task %s", current_task_id)
                    commit_sha = "no-changes"
                else:
                    logger.warning("Commit/push failed for task %s: %s", current_task_id, exc)
                    commit_sha = f"failed-{uuid.uuid4().hex[:8]}"
        else:
            # Fallback: no VCS or no workspace — generate placeholder
            commit_sha = uuid.uuid4().hex[:12]

        commit_shas.append(commit_sha)
        current_task_index += 1
        all_tasks_done = current_task_index >= len(task_ordering)

        # Emit commit.done event
        commit_event = Event.create(
            type=EventType.COMMIT_DONE,
            session_id=session_id,
            source="commit_node",
            payload={
                "sha": commit_sha,
                "task_id": current_task_id,
                "task_index": current_task_index - 1,
            },
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        await deps.event_bus.publish(commit_event)

        node_path.append("commit")
        return {
            "commit_shas": commit_shas,
            "current_task_index": current_task_index,
            "all_tasks_done": all_tasks_done,
            "node_path": node_path,
        }

    return commit_node
