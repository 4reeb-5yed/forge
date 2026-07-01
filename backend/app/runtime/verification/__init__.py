"""Verification Pipeline - advisory and blocking stage runners.

Runs advisory verifier stages concurrently, then blocking gate stages in
declared order. Emits `verify.stage` events per stage and `verify.passed`
when all blocking stages pass.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)


class StageStatus(str, Enum):
    """Status of a verification stage execution."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class StageResult:
    """Result of a single verification stage execution."""

    name: str
    status: StageStatus
    detail: str = ""


@dataclass
class VerificationStage:
    """Configuration for a single verification stage.

    Attributes:
        name: Unique stage name (e.g. 'lint', 'tests').
        timeout_s: Per-stage timeout in seconds.
        executor: Async callable that runs the stage and returns (passed: bool, detail: str).
    """

    name: str
    timeout_s: float
    executor: Callable[[], Awaitable[tuple[bool, str]]]


@dataclass
class PipelineResult:
    """Result of a full verification pipeline run.

    Attributes:
        advisory_results: Map of stage name -> StageResult for advisory stages.
        blocking_results: Map of stage name -> StageResult for blocking stages that ran.
        all_blocking_passed: True if every blocking stage passed.
        halted_at: Name of the blocking stage that failed (if any).
        policy_request: Information for the PolicyEngine if a blocking stage failed.
    """

    advisory_results: dict[str, StageResult] = field(default_factory=dict)
    blocking_results: dict[str, StageResult] = field(default_factory=dict)
    all_blocking_passed: bool = False
    halted_at: str | None = None
    policy_request: dict[str, Any] | None = None


# Type alias for the event emission callback
EventEmitter = Callable[[Event], Awaitable[Any]]


class VerificationPipeline:
    """Staged verification pipeline with advisory and blocking stages.

    Advisory stages run concurrently with individual timeouts. Their results
    are merged into a deterministic map keyed by stage name (order-independent).

    Blocking stages run sequentially in declared order after advisory merge.
    On blocking failure, the pipeline halts and requests a policy decision.

    Events emitted:
        - verify.stage: per stage (passed/failed/timed_out)
        - verify.passed: when all blocking stages pass
    """

    def __init__(
        self,
        *,
        advisory_stages: list[VerificationStage] | None = None,
        blocking_stages: list[VerificationStage] | None = None,
        event_emitter: EventEmitter | None = None,
        session_id: str = "",
        task_id: str = "",
    ) -> None:
        """Initialize the verification pipeline.

        Args:
            advisory_stages: List of advisory stages to run concurrently.
            blocking_stages: List of blocking stages to run in declared order.
            event_emitter: Async callable that publishes Event objects.
            session_id: Session ID for emitted events.
            task_id: Task ID for emitted events.
        """
        self._advisory_stages = advisory_stages or []
        self._blocking_stages = blocking_stages or []
        self._event_emitter = event_emitter
        self._session_id = session_id
        self._task_id = task_id

    async def run(self) -> PipelineResult:
        """Execute the full verification pipeline.

        1. Run all advisory stages concurrently with per-stage timeout.
        2. Merge advisory results into a dict keyed by stage name.
        3. Run blocking stages in declared order.
        4. Halt on first blocking failure; request policy decision.
        5. Emit verify.passed if all blocking stages pass.

        Returns:
            PipelineResult with advisory and blocking results.
        """
        result = PipelineResult()

        # Phase 1: Advisory stages (concurrent, non-blocking)
        result.advisory_results = await self._run_advisory_stages()

        # Phase 2: Blocking stages (sequential, in declared order)
        blocking_results, all_passed, halted_at = await self._run_blocking_stages()
        result.blocking_results = blocking_results
        result.all_blocking_passed = all_passed
        result.halted_at = halted_at

        if halted_at is not None:
            # Request policy decision for the failed blocking stage
            result.policy_request = {
                "stage": halted_at,
                "task_id": self._task_id,
                "action": "request_policy_decision",
            }
        elif all_passed:
            # All blocking stages passed — emit verify.passed
            await self._emit_verify_passed()

        return result

    async def _run_advisory_stages(self) -> dict[str, StageResult]:
        """Run all advisory stages concurrently with per-stage timeouts.

        Failed or timed-out stages are recorded but do not halt execution.
        Returns results merged into a deterministic dict keyed by stage name.
        """
        if not self._advisory_stages:
            return {}

        # Launch all advisory stages concurrently
        tasks = [
            self._run_single_advisory_stage(stage)
            for stage in self._advisory_stages
        ]
        stage_results = await asyncio.gather(*tasks)

        # Merge into deterministic dict keyed by stage name (sorted for consistency)
        results: dict[str, StageResult] = {}
        for stage_result in stage_results:
            results[stage_result.name] = stage_result

        return results

    async def _run_single_advisory_stage(self, stage: VerificationStage) -> StageResult:
        """Run a single advisory stage with its configured timeout.

        On timeout or failure, records the result and continues.
        Emits verify.stage event regardless of outcome.
        """
        try:
            passed, detail = await asyncio.wait_for(
                stage.executor(), timeout=stage.timeout_s
            )
            status = StageStatus.PASSED if passed else StageStatus.FAILED
            result = StageResult(name=stage.name, status=status, detail=detail)
        except asyncio.TimeoutError:
            result = StageResult(
                name=stage.name,
                status=StageStatus.TIMED_OUT,
                detail=f"Stage timed out after {stage.timeout_s}s",
            )
        except Exception as exc:
            result = StageResult(
                name=stage.name,
                status=StageStatus.FAILED,
                detail=f"Stage raised exception: {exc}",
            )

        await self._emit_stage_event(result)
        return result

    async def _run_blocking_stages(
        self,
    ) -> tuple[dict[str, StageResult], bool, str | None]:
        """Run blocking stages in declared order.

        Halts on first failure/timeout and returns.

        Returns:
            Tuple of (results dict, all_passed flag, halted_at stage name or None).
        """
        results: dict[str, StageResult] = {}

        if not self._blocking_stages:
            return results, True, None

        for stage in self._blocking_stages:
            try:
                passed, detail = await asyncio.wait_for(
                    stage.executor(), timeout=stage.timeout_s
                )
                status = StageStatus.PASSED if passed else StageStatus.FAILED
                result = StageResult(name=stage.name, status=status, detail=detail)
            except asyncio.TimeoutError:
                result = StageResult(
                    name=stage.name,
                    status=StageStatus.TIMED_OUT,
                    detail=f"Stage timed out after {stage.timeout_s}s",
                )
            except Exception as exc:
                result = StageResult(
                    name=stage.name,
                    status=StageStatus.FAILED,
                    detail=f"Stage raised exception: {exc}",
                )

            results[result.name] = result
            await self._emit_stage_event(result)

            # Halt on failure or timeout
            if result.status != StageStatus.PASSED:
                return results, False, result.name

        return results, True, None

    async def _emit_stage_event(self, result: StageResult) -> None:
        """Emit a verify.stage event for a completed stage."""
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.VERIFY_STAGE,
            session_id=self._session_id,
            source="verification_pipeline",
            correlation_id=self._session_id,
            payload={
                "task_id": self._task_id,
                "stage_name": result.name,
                "status": result.status.value,
                "detail": result.detail,
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit verify.stage event for stage %s", result.name
            )

    async def _emit_verify_passed(self) -> None:
        """Emit a verify.passed event when all blocking stages pass."""
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.VERIFY_PASSED,
            session_id=self._session_id,
            source="verification_pipeline",
            correlation_id=self._session_id,
            payload={
                "task_id": self._task_id,
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )

        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception(
                "Failed to emit verify.passed event for task %s", self._task_id
            )
