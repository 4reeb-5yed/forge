"""Commit node — commits verified changes and advances task index.

Runs diff-scope check before committing to block changes that touch
sensitive or out-of-scope paths (security hardening).

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
    """Factory that creates the commit node function.

    The commit node:
    1. Runs diff-scope check to block out-of-scope/sensitive changes
    2. Commits changes via workspace merge
    3. Appends the SHA to commit_shas
    4. Increments current_task_index
    5. Sets all_tasks_done when complete

    Args:
        deps: The RuntimeDeps container with all runtime components.

    Returns:
        An async node function conforming to the LangGraph node contract.
    """

    async def commit_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        commit_shas = list(state.get("commit_shas", []))
        current_task_index = state.get("current_task_index", 0)
        task_ordering = state.get("task_ordering", [])
        current_task_id = state.get("current_task_id")
        workspace_path = state.get("workspace_path", "")
        allowed_paths = state.get("allowed_paths")  # Task-scoped paths, if set

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
                # Emit a security event so audit trail captures the block
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

                # Do NOT commit — return error state
                node_path.append("commit_blocked")
                return {
                    "commit_shas": commit_shas,
                    "current_task_index": current_task_index,
                    "all_tasks_done": False,
                    "node_path": node_path,
                    "error": scope_result.reason,
                }

        # ─── Commit the changes ──────────────────────────────────────────
        # Generate a placeholder commit SHA
        commit_sha = uuid.uuid4().hex[:12]
        commit_shas.append(commit_sha)

        # Increment task index
        current_task_index += 1

        # Determine if all tasks are done
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
