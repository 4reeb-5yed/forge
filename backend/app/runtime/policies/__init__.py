"""Policy Engine - retry/escalate/skip decisions on verification failure.

A YAML-driven rules component that decides retry, escalate, or skip on
verification failure, bounded by a retry budget. Deterministic: same inputs
always produce the same decision.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType

logger = logging.getLogger(__name__)


class PolicyDecision(str, Enum):
    """Possible decisions the PolicyEngine can make."""

    RETRY = "retry"
    ESCALATE = "escalate"
    SKIP = "skip"
    FAILED = "failed"


@dataclass(frozen=True)
class PolicyRule:
    """A single rule from policies.yaml."""

    on: str  # "verification_failure", "tool_failure", "budget_exceeded"
    decision: str  # "retry", "escalate", "skip"
    stage: str | None = None  # Only for verification_failure rules
    max_attempts: int | None = None
    threshold: int | None = None


@dataclass(frozen=True)
class PolicyConfig:
    """Parsed policies.yaml configuration."""

    max_retries_per_task: int = 3
    escalation_failure_threshold: int = 2
    rules: list[PolicyRule] = field(default_factory=list)


@dataclass(frozen=True)
class PolicyResult:
    """Result of a policy decision.

    Contains the decision itself plus all context needed to emit events
    and write audit records.
    """

    decision: PolicyDecision
    task_id: str
    stage: str
    attempt_count: int
    rule_matched: str  # Which rule was applied
    reason: str  # Human-readable reason
    escalation_target: str | None = None  # The tool escalated to, if any
    task_failed: bool = False  # Whether this decision marks the task as failed


# Type alias for the event emission callback
EventEmitter = Callable[[Event], Awaitable[Any]]

# Type alias for registry capability check
RegistryChecker = Callable[[str], bool]


def parse_policy_config(raw: dict[str, Any]) -> PolicyConfig:
    """Parse a raw policies.yaml dict into a PolicyConfig.

    Args:
        raw: Dictionary loaded from policies.yaml.

    Returns:
        Typed PolicyConfig with validated fields.
    """
    retry_budget = raw.get("retry_budget", {})
    max_retries = retry_budget.get("max_retries_per_task", 3)

    escalation = raw.get("escalation", {})
    failure_threshold = escalation.get("failure_threshold", 2)

    rules: list[PolicyRule] = []
    for rule_dict in raw.get("rules", []):
        rules.append(
            PolicyRule(
                on=rule_dict.get("on", ""),
                decision=rule_dict.get("decision", "skip"),
                stage=rule_dict.get("stage"),
                max_attempts=rule_dict.get("max_attempts"),
                threshold=rule_dict.get("threshold"),
            )
        )

    return PolicyConfig(
        max_retries_per_task=max_retries,
        escalation_failure_threshold=failure_threshold,
        rules=rules,
    )


class PolicyEngine:
    """YAML-driven rules engine for retry/escalate/skip decisions.

    Deterministic: identical inputs always produce identical decisions.
    Each decide() call:
      1. Computes exactly one decision from rules + attempt count
      2. Emits exactly one policy.decision event
      3. Writes one DecisionRecord

    On escalation: checks registry for alternate coding tool.
    On retry budget exhaustion: marks task as failed.
    On task failure: emits task.fail event.
    """

    def __init__(
        self,
        *,
        config: PolicyConfig,
        event_emitter: EventEmitter | None = None,
        registry_checker: RegistryChecker | None = None,
        session_id: str = "",
    ) -> None:
        """Initialize the PolicyEngine.

        Args:
            config: Parsed PolicyConfig from policies.yaml.
            event_emitter: Async callable that publishes Event objects.
            registry_checker: Callable that checks if a capability name is available
                              in the registry. Returns True if available.
            session_id: Session ID for emitted events.
        """
        self._config = config
        self._event_emitter = event_emitter
        self._registry_checker = registry_checker
        self._session_id = session_id
        self._decisions: list[DecisionRecord] = []
        self._failed_tasks: set[str] = set()

    @property
    def decisions(self) -> list[DecisionRecord]:
        """Read-only access to accumulated decision records."""
        return list(self._decisions)

    def is_task_failed(self, task_id: str) -> bool:
        """Check if a task has been marked as failed."""
        return task_id in self._failed_tasks

    def can_execute(self, task_id: str, depends_on: list[str] | None = None) -> bool:
        """Check if a task can be executed (Req 8.6).

        A task can execute if:
        - It is not itself marked as failed
        - None of its dependencies are marked as failed

        Args:
            task_id: The task to check.
            depends_on: List of task IDs this task depends on.

        Returns:
            True if the task can be executed.
        """
        if self.is_task_failed(task_id):
            return False

        if depends_on:
            for dep in depends_on:
                if self.is_task_failed(dep):
                    return False

        return True

    async def decide(
        self,
        *,
        task_id: str,
        stage_name: str,
        attempt_count: int,
        tool_failures: int = 0,
    ) -> PolicyResult:
        """Make a deterministic policy decision for a failed stage.

        Evaluates rules in priority order:
          1. Check if retry budget is exhausted -> failed
          2. Check if tool failures hit escalation threshold -> escalate
          3. Match stage-specific rules -> retry/skip based on max_attempts
          4. Default -> skip

        Args:
            task_id: The task that failed.
            stage_name: The verification stage that failed.
            attempt_count: Current attempt number for this task (1-based).
            tool_failures: Number of consecutive tool failures for this task.

        Returns:
            PolicyResult with the decision and all context.
        """
        result = self._compute_decision(
            task_id=task_id,
            stage_name=stage_name,
            attempt_count=attempt_count,
            tool_failures=tool_failures,
        )

        # Track failed tasks (Req 8.6)
        if result.task_failed:
            self._failed_tasks.add(task_id)

        # Write DecisionRecord (Req 8.3)
        record = self._build_decision_record(result)
        self._decisions.append(record)

        # Emit exactly one policy.decision event (Req 8.2)
        await self._emit_policy_decision_event(result)

        # On task failure, emit task.fail event (Req 8.7)
        if result.task_failed:
            await self._emit_task_fail_event(result)

        return result

    def _compute_decision(
        self,
        *,
        task_id: str,
        stage_name: str,
        attempt_count: int,
        tool_failures: int,
    ) -> PolicyResult:
        """Deterministically compute the policy decision.

        Priority order:
          1. Retry budget exhausted -> failed
          2. Tool failure threshold reached -> escalate (with registry check)
          3. Stage-specific rule match -> retry or skip based on max_attempts
          4. Default -> skip
        """
        # Priority 1: Retry budget exhaustion (Req 8.6)
        if attempt_count >= self._config.max_retries_per_task:
            return PolicyResult(
                decision=PolicyDecision.FAILED,
                task_id=task_id,
                stage=stage_name,
                attempt_count=attempt_count,
                rule_matched="retry_budget.max_retries_per_task",
                reason=(
                    f"Retry budget exhausted: attempt {attempt_count} "
                    f">= max {self._config.max_retries_per_task}"
                ),
                task_failed=True,
            )

        # Priority 2: Tool failure escalation (Req 8.4)
        if tool_failures >= self._config.escalation_failure_threshold:
            return self._handle_escalation(
                task_id=task_id,
                stage_name=stage_name,
                attempt_count=attempt_count,
                tool_failures=tool_failures,
            )

        # Priority 3: Stage-specific rule match (Req 8.1)
        for rule in self._config.rules:
            if rule.on == "verification_failure" and rule.stage == stage_name:
                if rule.max_attempts is not None and attempt_count >= rule.max_attempts:
                    # Stage-specific retry budget exhausted -> failed
                    return PolicyResult(
                        decision=PolicyDecision.FAILED,
                        task_id=task_id,
                        stage=stage_name,
                        attempt_count=attempt_count,
                        rule_matched=f"rule[stage={stage_name}].max_attempts",
                        reason=(
                            f"Stage '{stage_name}' max attempts reached: "
                            f"attempt {attempt_count} >= max {rule.max_attempts}"
                        ),
                        task_failed=True,
                    )

                # Rule says retry or skip
                decision = PolicyDecision(rule.decision)
                return PolicyResult(
                    decision=decision,
                    task_id=task_id,
                    stage=stage_name,
                    attempt_count=attempt_count,
                    rule_matched=f"rule[on={rule.on}, stage={stage_name}]",
                    reason=(
                        f"Rule matched: {rule.on} on stage '{stage_name}' -> {rule.decision}"
                    ),
                )

        # Priority 4: Default -> skip
        return PolicyResult(
            decision=PolicyDecision.SKIP,
            task_id=task_id,
            stage=stage_name,
            attempt_count=attempt_count,
            rule_matched="default",
            reason=f"No matching rule for stage '{stage_name}'; defaulting to skip",
        )

    def _handle_escalation(
        self,
        *,
        task_id: str,
        stage_name: str,
        attempt_count: int,
        tool_failures: int,
    ) -> PolicyResult:
        """Handle escalation to alternate coding tool.

        Checks registry for alternate tool availability (Req 8.5).
        If unavailable, marks task as failed.
        """
        alternate_tool = "tool_openhands"  # The alternate coding tool

        # Check if alternate coding tool is available in registry (Req 8.5)
        tool_available = False
        if self._registry_checker is not None:
            tool_available = self._registry_checker(alternate_tool)

        if not tool_available:
            # No alternate tool available -> mark task failed (Req 8.5)
            return PolicyResult(
                decision=PolicyDecision.FAILED,
                task_id=task_id,
                stage=stage_name,
                attempt_count=attempt_count,
                rule_matched="escalation.failure_threshold",
                reason=(
                    f"Escalation triggered (tool_failures={tool_failures} "
                    f">= threshold={self._config.escalation_failure_threshold}) "
                    f"but alternate coding tool '{alternate_tool}' is unavailable in registry"
                ),
                escalation_target=alternate_tool,
                task_failed=True,
            )

        # Alternate tool available -> escalate (Req 8.4)
        return PolicyResult(
            decision=PolicyDecision.ESCALATE,
            task_id=task_id,
            stage=stage_name,
            attempt_count=attempt_count,
            rule_matched="escalation.failure_threshold",
            reason=(
                f"Tool failures ({tool_failures}) reached escalation threshold "
                f"({self._config.escalation_failure_threshold}); "
                f"escalating to '{alternate_tool}'"
            ),
            escalation_target=alternate_tool,
        )

    def _build_decision_record(self, result: PolicyResult) -> DecisionRecord:
        """Build a DecisionRecord from a PolicyResult (Req 8.3).

        Rationale is deterministic: constructed from inputs only.
        """
        # Determine DecisionKind based on decision
        kind_map = {
            PolicyDecision.RETRY: DecisionKind.RETRY,
            PolicyDecision.ESCALATE: DecisionKind.ESCALATE,
            PolicyDecision.SKIP: DecisionKind.SKIP,
            PolicyDecision.FAILED: DecisionKind.TASK_FAILED,
        }
        kind = kind_map[result.decision]

        # Build alternatives list
        all_decisions = [d.value for d in PolicyDecision if d != result.decision]

        return DecisionRecord(
            kind=kind,
            subject=result.task_id,
            inputs={
                "task_id": result.task_id,
                "stage": result.stage,
                "attempt_count": result.attempt_count,
                "rule_matched": result.rule_matched,
            },
            decision=result.decision.value,
            rationale=result.reason,
            alternatives=all_decisions,
            session_id=self._session_id,
        )

    async def _emit_policy_decision_event(self, result: PolicyResult) -> None:
        """Emit exactly one policy.decision event (Req 8.2)."""
        if self._event_emitter is None:
            return

        payload: dict[str, Any] = {
            "task_id": result.task_id,
            "stage": result.stage,
            "decision": result.decision.value,
            "rule_matched": result.rule_matched,
            "attempt_count": result.attempt_count,
            "reason": result.reason,
        }
        if result.escalation_target:
            payload["escalation_target"] = result.escalation_target

        event = Event.create(
            type=EventType.POLICY_DECISION,
            session_id=self._session_id,
            source="policy_engine",
            payload=payload,
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit policy.decision event for task %s", result.task_id
            )

    async def _emit_task_fail_event(self, result: PolicyResult) -> None:
        """Emit task.fail event when a task is marked failed (Req 8.7).

        Carries: task_id, failing stage, reason, and policy decision.
        """
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.TASK_FAIL,
            session_id=self._session_id,
            source="policy_engine",
            payload={
                "task_id": result.task_id,
                "stage": result.stage,
                "reason": result.reason,
                "policy_decision": result.decision.value,
            },
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit task.fail event for task %s", result.task_id
            )
