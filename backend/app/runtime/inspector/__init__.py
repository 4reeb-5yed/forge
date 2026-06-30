"""Runtime Inspector — query-only facade for runtime state and explainability.

The RuntimeInspector answers "what is Forge doing?" and "why?" by reading
structured state from the AuditTrail, CapabilityRegistry, and runtime state.
It never invokes an AI model.

Per design R3: the Inspector is a read-only query facade over AuditTrail +
CapabilityRegistry + live ForgeState. It owns no storage.

Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6, 20.7
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.runtime.audit import AuditTrail
from app.runtime.budget import SessionBudget
from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType
from app.runtime.models import ForgeState, Task
from app.runtime.registry import CapabilityRegistry

logger = logging.getLogger(__name__)


class TaskNotFoundError(Exception):
    """Raised when a requested task identifier does not exist in the session.

    Per requirement 20.6: return a not-found response carrying the requested
    task identifier.
    """

    def __init__(self, task_id: str, session_id: str) -> None:
        self.task_id = task_id
        self.session_id = session_id
        super().__init__(
            f"Task not found: task_id={task_id} in session={session_id}"
        )


class DecisionNotFoundError(Exception):
    """Raised when no DecisionRecord exists for a session.

    Per requirement 20.4: return a not-found response indicating that
    no decision has been recorded for the session.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"No decision record found for session: {session_id}"
        )


@dataclass(frozen=True)
class WorkerStatus:
    """Status of a task execution worker."""

    worker_id: str = "worker-0"
    status: str = "idle"  # idle, executing, verifying
    current_task_id: str | None = None


@dataclass(frozen=True)
class TaskView:
    """Read-only view of a task for inspector responses."""

    id: str
    title: str
    status: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    attempts: int = 0


@dataclass(frozen=True)
class TaskExplanation:
    """Reconstructed history of a task via causation links.

    Assembled by following causation_id links in the AuditTrail. Includes
    model selections, verifier outcomes, retries, and resulting commit.
    """

    task_id: str
    model_selections: list[dict[str, Any]] = field(default_factory=list)
    verifier_outcomes: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)
    commit: dict[str, Any] | None = None
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeStatus:
    """Complete runtime status response.

    Returned by get_status() — combines current node, worker status,
    task queue, and active task into a single response object.
    """

    current_node: str
    worker_status: WorkerStatus
    task_queue: list[TaskView]
    active_task: TaskView | None
    budget: dict[str, Any] | None = None


class RuntimeInspector:
    """Query-only facade answering 'what' and 'why' from structured state.

    Derives ALL responses from:
    - Runtime state (ForgeState, task dispatcher state)
    - AuditTrail (DecisionRecords, event log)
    - CapabilityRegistry (capability summary)

    Never invokes an AI model (Requirement 20.7).

    Requirements:
        20.1 — Return current node, worker status, task queue, active task (within 2s)
        20.2 — Return empty active task and empty queue when no build in progress
        20.3 — Return most recent DecisionRecord for session
        20.4 — Return not-found when no DecisionRecord exists
        20.5 — Reconstruct task history via causation links
        20.6 — Return not-found when task does not exist
        20.7 — Derive all responses from state, AuditTrail, Registry only (no AI)
    """

    def __init__(
        self,
        *,
        audit_trail: AuditTrail,
        registry: CapabilityRegistry,
        state_provider: _StateProvider | None = None,
    ) -> None:
        """Initialize the RuntimeInspector.

        Args:
            audit_trail: The AuditTrail instance for decision/event queries.
            registry: The CapabilityRegistry for capability state queries.
            state_provider: Optional provider for runtime state. If None,
                            a default empty state provider is used.
        """
        self._audit_trail = audit_trail
        self._registry = registry
        self._state_provider = state_provider or _StateProvider()

    # --- Public Query Methods ---

    def get_status(self, session_id: str) -> RuntimeStatus:
        """Return the current runtime status for a session.

        Requirement 20.1: Returns current node, worker status, task queue,
        and active task derived from runtime state, within 2 seconds.

        Requirement 20.2: If no build is in progress, returns current node,
        an empty active task, and an empty task queue.

        Args:
            session_id: The session to query.

        Returns:
            A RuntimeStatus object with all status fields populated.
        """
        state = self._state_provider.get_state(session_id)
        tasks = state.get("tasks", [])
        current_task_id = state.get("current_task_id")
        status = state.get("status", "idle")

        # Determine current node from state status
        current_node = self._derive_current_node(state)

        # Determine worker status
        worker_status = self._derive_worker_status(current_task_id, tasks)

        # Build task queue and active task
        # Req 20.2: empty queue/active task when no build in progress
        if not self._is_build_in_progress(state):
            task_queue: list[TaskView] = []
            active_task: TaskView | None = None
        else:
            task_queue = self._derive_task_queue(tasks)
            active_task = self._derive_active_task(current_task_id, tasks)

        # Budget info if available
        budget = self._state_provider.get_budget_summary(session_id)

        return RuntimeStatus(
            current_node=current_node,
            worker_status=worker_status,
            task_queue=task_queue,
            active_task=active_task,
            budget=budget,
        )

    def current_node(self, session_id: str) -> str:
        """Return the current workflow node for a session.

        Args:
            session_id: The session to query.

        Returns:
            The name of the current workflow node (e.g., "execute", "verify").
        """
        state = self._state_provider.get_state(session_id)
        return self._derive_current_node(state)

    def worker_status(self, session_id: str) -> WorkerStatus:
        """Return the worker status for a session.

        Args:
            session_id: The session to query.

        Returns:
            A WorkerStatus reflecting the current execution state.
        """
        state = self._state_provider.get_state(session_id)
        tasks = state.get("tasks", [])
        current_task_id = state.get("current_task_id")
        return self._derive_worker_status(current_task_id, tasks)

    def task_queue(self, session_id: str) -> list[TaskView]:
        """Return the pending task queue for a session.

        Requirement 20.2: returns empty list when no build in progress.

        Args:
            session_id: The session to query.

        Returns:
            List of TaskView objects for pending/queued tasks.
        """
        state = self._state_provider.get_state(session_id)
        if not self._is_build_in_progress(state):
            return []
        tasks = state.get("tasks", [])
        return self._derive_task_queue(tasks)

    def active_task(self, session_id: str) -> TaskView | None:
        """Return the currently active task for a session.

        Requirement 20.2: returns None when no build in progress.

        Args:
            session_id: The session to query.

        Returns:
            A TaskView for the active task, or None if no task is active.
        """
        state = self._state_provider.get_state(session_id)
        if not self._is_build_in_progress(state):
            return None
        tasks = state.get("tasks", [])
        current_task_id = state.get("current_task_id")
        return self._derive_active_task(current_task_id, tasks)

    def explain_last_decision(self, session_id: str) -> DecisionRecord:
        """Return the most recent DecisionRecord for a session.

        Requirement 20.3: Returns kind, subject, inputs, decision, rationale,
        and alternatives of the most recent decision.

        Requirement 20.4: Raises DecisionNotFoundError if no record exists.

        Args:
            session_id: The session to query.

        Returns:
            The most recent DecisionRecord for the session.

        Raises:
            DecisionNotFoundError: If no DecisionRecord exists for the session.
        """
        decisions = self._audit_trail.get_decisions(session_id)
        if not decisions:
            raise DecisionNotFoundError(session_id)
        return decisions[-1]

    def explain_task(self, session_id: str, task_id: str) -> TaskExplanation:
        """Reconstruct a task's history by following causation links.

        Requirement 20.5: Assembles model selections, verifier outcomes,
        retries, and resulting commit by following causation_id links in
        the AuditTrail.

        Requirement 20.6: Raises TaskNotFoundError if the task does not exist.

        Args:
            session_id: The session to query.
            task_id: The task identifier to explain.

        Returns:
            A TaskExplanation with the full task history.

        Raises:
            TaskNotFoundError: If the task_id does not exist in the session.
        """
        # Verify task exists in the session state
        state = self._state_provider.get_state(session_id)
        tasks = state.get("tasks", [])
        task_exists = any(self._task_id(t) == task_id for t in tasks)
        if not task_exists:
            raise TaskNotFoundError(task_id, session_id)

        # Reconstruct history from audit events and decisions
        events = self._audit_trail.get_events(session_id)
        decisions = self._audit_trail.get_decisions(session_id)

        model_selections: list[dict[str, Any]] = []
        verifier_outcomes: list[dict[str, Any]] = []
        retries: list[dict[str, Any]] = []
        commit: dict[str, Any] | None = None
        policy_decisions: list[dict[str, Any]] = []

        # Filter events related to this task
        for event in events:
            payload = event.payload
            event_task_id = payload.get("task_id", "")

            if event_task_id != task_id:
                continue

            if event.type == EventType.MODEL_SELECTED:
                model_selections.append({
                    "provider": payload.get("provider", ""),
                    "model": payload.get("model", ""),
                    "role": payload.get("role", ""),
                    "attempt": payload.get("attempt", 1),
                    "event_id": event.event_id,
                    "causation_id": event.causation_id,
                })
            elif event.type == EventType.MODEL_FALLBACK:
                model_selections.append({
                    "provider": payload.get("provider", ""),
                    "model": payload.get("model", ""),
                    "role": payload.get("role", ""),
                    "attempt": payload.get("attempt", 1),
                    "reason": payload.get("reason", ""),
                    "fallback": True,
                    "event_id": event.event_id,
                    "causation_id": event.causation_id,
                })
            elif event.type == EventType.VERIFY_STAGE:
                verifier_outcomes.append({
                    "stage": payload.get("stage_name", ""),
                    "status": payload.get("status", ""),
                    "detail": payload.get("detail", ""),
                    "event_id": event.event_id,
                    "causation_id": event.causation_id,
                })
            elif event.type == EventType.VERIFY_PASSED:
                verifier_outcomes.append({
                    "stage": "all_blocking",
                    "status": "passed",
                    "event_id": event.event_id,
                    "causation_id": event.causation_id,
                })
            elif event.type == EventType.COMMIT_DONE:
                commit = {
                    "sha": payload.get("sha", ""),
                    "files": payload.get("files", []),
                    "event_id": event.event_id,
                    "causation_id": event.causation_id,
                }
            elif event.type == EventType.POLICY_DECISION:
                policy_decisions.append({
                    "decision": payload.get("decision", ""),
                    "rule": payload.get("rule", ""),
                    "subject": payload.get("subject", ""),
                    "event_id": event.event_id,
                    "causation_id": event.causation_id,
                })

        # Filter decisions related to this task
        for decision in decisions:
            if decision.subject == task_id or task_id in decision.subject:
                if decision.kind in (
                    DecisionKind.RETRY,
                    DecisionKind.ESCALATE,
                    DecisionKind.SKIP,
                ):
                    retries.append({
                        "kind": decision.kind.value,
                        "decision": decision.decision,
                        "rationale": decision.rationale,
                        "alternatives": decision.alternatives,
                        "inputs": decision.inputs,
                    })

        return TaskExplanation(
            task_id=task_id,
            model_selections=model_selections,
            verifier_outcomes=verifier_outcomes,
            retries=retries,
            commit=commit,
            policy_decisions=policy_decisions,
        )

    def capability_summary(self) -> dict[str, Any]:
        """Return the current capability summary from the registry.

        Requirement 20.7: derived from CapabilityRegistry, no AI invoked.

        Returns:
            A dict representation of the CapabilitySummary.
        """
        summary = self._registry.summary()
        return {
            "available": {k.value: v for k, v in summary.available.items()},
            "degraded": [c.value for c in summary.degraded],
            "missing_required": [c.value for c in summary.missing_required],
            "missing_reasons": summary.missing_reasons,
            "soft_degradations": summary.soft_degradations,
            "mode": summary.mode,
            "can_operate": summary.can_operate,
        }

    # --- Private Helper Methods ---

    @staticmethod
    def _derive_current_node(state: dict[str, Any]) -> str:
        """Derive the current workflow node from state.

        Maps status strings to workflow node names. Falls back to "idle"
        if no meaningful mapping exists.
        """
        status = state.get("status", "idle")
        # Map session/build status to workflow node name
        status_to_node = {
            "created": "idle",
            "idle": "idle",
            "clarifying": "clarify",
            "planning": "plan",
            "executing": "execute",
            "verifying": "verify",
            "committing": "commit",
            "documenting": "doc_update",
            "finalizing": "finalize",
            "interrupted": "interrupt",
            "completed": "finalize",
            "failed": "idle",
            "stopped": "idle",
        }
        return status_to_node.get(status, "idle")

    @staticmethod
    def _derive_worker_status(
        current_task_id: str | None, tasks: list[Any]
    ) -> WorkerStatus:
        """Derive worker status from current task and task list."""
        if current_task_id is None:
            return WorkerStatus(worker_id="worker-0", status="idle")

        # Find the current task to check if verifying
        for task in tasks:
            task_id = task.id if isinstance(task, Task) else task.get("id", "")
            if task_id == current_task_id:
                task_status = (
                    task.status if isinstance(task, Task) else task.get("status", "")
                )
                if task_status == "verifying":
                    return WorkerStatus(
                        worker_id="worker-0",
                        status="verifying",
                        current_task_id=current_task_id,
                    )
                return WorkerStatus(
                    worker_id="worker-0",
                    status="executing",
                    current_task_id=current_task_id,
                )

        return WorkerStatus(
            worker_id="worker-0",
            status="executing",
            current_task_id=current_task_id,
        )

    @staticmethod
    def _is_build_in_progress(state: dict[str, Any]) -> bool:
        """Determine if a build is currently in progress."""
        status = state.get("status", "idle")
        active_statuses = {
            "clarifying",
            "planning",
            "executing",
            "verifying",
            "committing",
            "documenting",
            "finalizing",
            "interrupted",
        }
        return status in active_statuses

    @staticmethod
    def _derive_task_queue(tasks: list[Any]) -> list[TaskView]:
        """Derive the pending task queue from the task list."""
        queue: list[TaskView] = []
        for task in tasks:
            if isinstance(task, Task):
                if task.status == "pending":
                    queue.append(TaskView(
                        id=task.id,
                        title=task.title,
                        status=task.status,
                        description=task.description,
                        depends_on=task.depends_on,
                        attempts=task.attempts,
                    ))
            else:
                # dict-style task
                if task.get("status", "") == "pending":
                    queue.append(TaskView(
                        id=task.get("id", ""),
                        title=task.get("title", ""),
                        status=task.get("status", ""),
                        description=task.get("description", ""),
                        depends_on=task.get("depends_on", []),
                        attempts=task.get("attempts", 0),
                    ))
        return queue

    @staticmethod
    def _derive_active_task(
        current_task_id: str | None, tasks: list[Any]
    ) -> TaskView | None:
        """Derive the active task from current_task_id and task list."""
        if current_task_id is None:
            return None
        for task in tasks:
            if isinstance(task, Task):
                if task.id == current_task_id:
                    return TaskView(
                        id=task.id,
                        title=task.title,
                        status=task.status,
                        description=task.description,
                        depends_on=task.depends_on,
                        attempts=task.attempts,
                    )
            else:
                if task.get("id", "") == current_task_id:
                    return TaskView(
                        id=task.get("id", ""),
                        title=task.get("title", ""),
                        status=task.get("status", ""),
                        description=task.get("description", ""),
                        depends_on=task.get("depends_on", []),
                        attempts=task.get("attempts", 0),
                    )
        return None

    @staticmethod
    def _task_id(task: Any) -> str:
        """Extract task_id from either a Task object or a dict."""
        if isinstance(task, Task):
            return task.id
        return task.get("id", "")


class _StateProvider:
    """Provides access to runtime state for the inspector.

    This is a simple abstraction that can be replaced with actual
    workflow state lookups. Keeps the inspector decoupled from the
    specific state management implementation.
    """

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._budgets: dict[str, SessionBudget] = {}

    def set_state(self, session_id: str, state: dict[str, Any]) -> None:
        """Set/update the state for a session.

        Args:
            session_id: The session to update.
            state: The ForgeState-like dict to store.
        """
        self._states[session_id] = state

    def get_state(self, session_id: str) -> dict[str, Any]:
        """Get the current state for a session.

        Returns an empty state dict if no state is set (safe default for
        sessions with no build in progress per Req 20.2).

        Args:
            session_id: The session to query.

        Returns:
            The state dict, or a default empty state.
        """
        return self._states.get(session_id, {
            "status": "idle",
            "tasks": [],
            "current_task_id": None,
        })

    def set_budget(self, session_id: str, budget: SessionBudget) -> None:
        """Associate a SessionBudget with a session.

        Args:
            session_id: The session to associate.
            budget: The SessionBudget instance.
        """
        self._budgets[session_id] = budget

    def get_budget_summary(self, session_id: str) -> dict[str, Any] | None:
        """Get the budget summary for a session if available.

        Args:
            session_id: The session to query.

        Returns:
            Budget summary dict or None if no budget is set.
        """
        budget = self._budgets.get(session_id)
        if budget is None:
            return None
        return budget.summary()
