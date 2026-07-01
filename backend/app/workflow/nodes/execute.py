"""Execute node — clones repo, runs coding tool, produces real changes.

This is the REAL execute node that:
1. Clones the target repo into an isolated workspace
2. Invokes the coding tool (Aider) to make changes
3. Stores workspace_path for the commit node to push

Requirements: 8.1, 8.2, 8.3
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType
from app.runtime.models import ForgeState, Task
from app.workflow.deps import RuntimeDeps

logger = logging.getLogger(__name__)

NodeFn = Callable[[ForgeState], Awaitable[dict[str, Any]]]


def make_execute_node(deps: RuntimeDeps) -> NodeFn:
    """Factory that creates the execute node function."""

    async def execute_node(state: ForgeState) -> dict[str, Any]:
        node_path = list(state.get("node_path", []))
        session_id = state.get("session_id", "")
        task_ordering = state.get("task_ordering", [])
        current_task_index = state.get("current_task_index", 0)
        tasks = list(state.get("tasks", []))
        errors = list(state.get("errors", []))

        # Get current task
        current_task_id = task_ordering[current_task_index] if task_ordering else None
        if current_task_id is None:
            node_path.append("execute")
            return {"node_path": node_path}

        # Find task description
        task_description = ""
        task_target_files: list[str] = []
        for task in tasks:
            tid = task.id if isinstance(task, Task) else task.get("id", "")
            if tid == current_task_id:
                task_description = (
                    task.description if isinstance(task, Task)
                    else task.get("description", task.get("title", ""))
                )
                task_target_files = (
                    task.target_files if isinstance(task, Task)
                    else task.get("target_files", [])
                )
                break

        if not task_description:
            task_description = f"Execute task {current_task_id}"

        # Create workspace
        workspace_info = await deps.workspace_manager.create(
            task_id=current_task_id,
            session_id=session_id,
        )
        workspace_path = workspace_info.path

        # ─── Clone the repo into workspace ────────────────────────────────
        # Get repo_url from session context or state
        repo_url = state.get("repo_url", "")
        logger.info("Execute node: repo_url=%s, vcs=%s", repo_url, deps.vcs)
        if repo_url and deps.vcs is not None:
            try:
                # Clone into workspace (shallow, default branch)
                logger.info("Cloning %s into %s", repo_url, workspace_path)
                await deps.vcs.clone(
                    url=repo_url,
                    ref="main",
                    dest_path=workspace_path,
                )
                logger.info("Clone successful for %s", repo_url)
            except RuntimeError as exc:
                logger.warning("Clone failed (main branch) for %s: %s", repo_url, exc)
                # Try 'master' branch if 'main' fails
                try:
                    await deps.vcs.clone(
                        url=repo_url,
                        ref="master",
                        dest_path=workspace_path,
                    )
                    logger.info("Clone successful for %s (master)", repo_url)
                except RuntimeError:
                    logger.warning("Clone failed for %s: %s", repo_url, exc)
                    errors.append({
                        "code": "clone_failed",
                        "message": str(exc),
                        "node": "execute",
                        "task_id": current_task_id,
                    })
        else:
            if not repo_url:
                logger.info("No repo_url in state — running Aider in empty workspace")

        # Emit task start
        start_event = Event.create(
            type=EventType.TASK_START,
            session_id=session_id,
            source="execute_node",
            payload={
                "task_id": current_task_id,
                "workspace_path": workspace_path,
            },
            correlation_id=session_id,
            event_id=f"task-start-{current_task_id}",
        )
        await deps.event_bus.publish(start_event)

        # ─── Run the coding tool ─────────────────────────────────────────
        tool_success = False
        if deps.coding_tool is not None:
            try:
                # Pass repo_url for tools that work directly on GitHub (like OpenHands)
                if hasattr(deps.coding_tool, 'execute'):
                    import inspect
                    sig = inspect.signature(deps.coding_tool.execute)
                    if 'repo_url' in sig.parameters:
                        result = await deps.coding_tool.execute(
                            task_description=task_description,
                            workspace_path=workspace_path,
                            repo_url=repo_url,
                        )
                    else:
                        result = await deps.coding_tool.execute(
                            task_description=task_description,
                            workspace_path=workspace_path,
                        )
                else:
                    result = await deps.coding_tool.execute(
                        task_description=task_description,
                        workspace_path=workspace_path,
                    )
                tool_success = result.success
                if not result.success:
                    logger.warning("Coding tool failed: %s", result.error)
                    errors.append({
                        "code": "tool_execution_failed",
                        "message": result.error,
                        "node": "execute",
                        "task_id": current_task_id,
                    })
            except Exception as exc:
                logger.exception("Coding tool raised for task %s", current_task_id)
                errors.append({
                    "code": "tool_exception",
                    "message": str(exc),
                    "node": "execute",
                    "task_id": current_task_id,
                })
        else:
            logger.warning("No coding tool — task %s is a no-op", current_task_id)
            tool_success = True

        # Update task status
        new_status = "verifying" if tool_success else "failed"
        for i, task in enumerate(tasks):
            tid = task.id if isinstance(task, Task) else task.get("id", "")
            if tid == current_task_id:
                if isinstance(task, Task):
                    tasks[i] = Task(
                        id=task.id,
                        title=task.title,
                        description=task.description,
                        depends_on=task.depends_on,
                        target_files=task.target_files,
                        status=new_status,
                        attempts=task.attempts + 1,
                        assigned_tool=task.assigned_tool,
                        workspace_id=workspace_info.workspace_id,
                    )
                break

        node_path.append("execute")
        return {
            "current_task_id": current_task_id,
            "workspace_path": workspace_path,
            "allowed_paths": task_target_files or None,
            "tasks": tasks,
            "errors": errors,
            "node_path": node_path,
        }

    return execute_node
