"""Unit tests for the VerificationPipeline with advisory and blocking stages.

Tests concurrent advisory execution, blocking sequential gate, event emission,
timeout handling, and policy decision requests.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.runtime.events.models import Event, EventType
from app.runtime.verification import (
    PipelineResult,
    StageResult,
    StageStatus,
    VerificationPipeline,
    VerificationStage,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeEventCollector:
    """Collects emitted events for assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_passing_stage(name: str, timeout_s: float = 10.0, detail: str = "") -> VerificationStage:
    """Create an advisory/blocking stage that passes."""

    async def executor() -> tuple[bool, str]:
        return True, detail or f"{name} passed"

    return VerificationStage(name=name, timeout_s=timeout_s, executor=executor)


def make_failing_stage(name: str, timeout_s: float = 10.0, detail: str = "") -> VerificationStage:
    """Create an advisory/blocking stage that fails."""

    async def executor() -> tuple[bool, str]:
        return False, detail or f"{name} failed"

    return VerificationStage(name=name, timeout_s=timeout_s, executor=executor)


def make_slow_stage(name: str, delay: float, timeout_s: float = 1.0) -> VerificationStage:
    """Create a stage that takes longer than its timeout."""

    async def executor() -> tuple[bool, str]:
        await asyncio.sleep(delay)
        return True, f"{name} completed"

    return VerificationStage(name=name, timeout_s=timeout_s, executor=executor)


def make_error_stage(name: str, error: str = "boom") -> VerificationStage:
    """Create a stage that raises an exception."""

    async def executor() -> tuple[bool, str]:
        raise RuntimeError(error)

    return VerificationStage(name=name, timeout_s=10.0, executor=executor)


# ---------------------------------------------------------------------------
# Tests: Advisory stages run concurrently (Req 7.1)
# ---------------------------------------------------------------------------


class TestAdvisoryStagesConcurrent:
    """Advisory stages run concurrently with per-stage timeout."""

    async def test_all_advisory_pass(self) -> None:
        """All advisory stages pass and results are collected."""
        pipeline = VerificationPipeline(
            advisory_stages=[
                make_passing_stage("lint"),
                make_passing_stage("type_check"),
                make_passing_stage("security"),
            ],
            blocking_stages=[],
        )

        result = await pipeline.run()

        assert len(result.advisory_results) == 3
        assert result.advisory_results["lint"].status == StageStatus.PASSED
        assert result.advisory_results["type_check"].status == StageStatus.PASSED
        assert result.advisory_results["security"].status == StageStatus.PASSED

    async def test_advisory_stages_run_concurrently(self) -> None:
        """Advisory stages actually run concurrently (total time < sum of delays)."""
        delay = 0.1

        async def slow_pass() -> tuple[bool, str]:
            await asyncio.sleep(delay)
            return True, "done"

        stages = [
            VerificationStage(name=f"s{i}", timeout_s=5.0, executor=slow_pass)
            for i in range(5)
        ]
        pipeline = VerificationPipeline(advisory_stages=stages, blocking_stages=[])

        import time
        start = time.monotonic()
        result = await pipeline.run()
        elapsed = time.monotonic() - start

        assert len(result.advisory_results) == 5
        # If sequential, would take 5 * 0.1 = 0.5s. Concurrent should be ~0.1s
        assert elapsed < 0.3  # generous margin


# ---------------------------------------------------------------------------
# Tests: Advisory failure/timeout continue (Req 7.2)
# ---------------------------------------------------------------------------


class TestAdvisoryFailureContinues:
    """Failed/timed-out advisory stages are recorded; pipeline continues."""

    async def test_advisory_failure_recorded_and_continues(self) -> None:
        """A failed advisory stage is recorded but doesn't halt others."""
        pipeline = VerificationPipeline(
            advisory_stages=[
                make_passing_stage("lint"),
                make_failing_stage("type_check"),
                make_passing_stage("security"),
            ],
            blocking_stages=[],
        )

        result = await pipeline.run()

        assert len(result.advisory_results) == 3
        assert result.advisory_results["lint"].status == StageStatus.PASSED
        assert result.advisory_results["type_check"].status == StageStatus.FAILED
        assert result.advisory_results["security"].status == StageStatus.PASSED

    async def test_advisory_timeout_recorded_and_continues(self) -> None:
        """A timed-out advisory stage is recorded but doesn't halt others."""
        pipeline = VerificationPipeline(
            advisory_stages=[
                make_passing_stage("lint"),
                make_slow_stage("type_check", delay=5.0, timeout_s=0.05),
                make_passing_stage("security"),
            ],
            blocking_stages=[],
        )

        result = await pipeline.run()

        assert len(result.advisory_results) == 3
        assert result.advisory_results["lint"].status == StageStatus.PASSED
        assert result.advisory_results["type_check"].status == StageStatus.TIMED_OUT
        assert result.advisory_results["security"].status == StageStatus.PASSED

    async def test_advisory_exception_recorded_as_failed(self) -> None:
        """An advisory stage that raises an exception is recorded as failed."""
        pipeline = VerificationPipeline(
            advisory_stages=[
                make_passing_stage("lint"),
                make_error_stage("type_check", error="import error"),
            ],
            blocking_stages=[],
        )

        result = await pipeline.run()

        assert result.advisory_results["type_check"].status == StageStatus.FAILED
        assert "import error" in result.advisory_results["type_check"].detail


# ---------------------------------------------------------------------------
# Tests: Advisory merge is order-independent (Req 7.3)
# ---------------------------------------------------------------------------


class TestAdvisoryMergeOrderIndependence:
    """Advisory results are merged into a dict keyed by stage name."""

    async def test_results_keyed_by_stage_name(self) -> None:
        """Results are a dict keyed by stage name regardless of completion order."""
        # Use varying delays to ensure different completion orders
        async def fast() -> tuple[bool, str]:
            return True, "fast"

        async def slow() -> tuple[bool, str]:
            await asyncio.sleep(0.05)
            return True, "slow"

        pipeline = VerificationPipeline(
            advisory_stages=[
                VerificationStage(name="slow_one", timeout_s=5.0, executor=slow),
                VerificationStage(name="fast_one", timeout_s=5.0, executor=fast),
            ],
            blocking_stages=[],
        )

        result = await pipeline.run()

        # Both present regardless of completion order
        assert "slow_one" in result.advisory_results
        assert "fast_one" in result.advisory_results
        assert result.advisory_results["slow_one"].name == "slow_one"
        assert result.advisory_results["fast_one"].name == "fast_one"


# ---------------------------------------------------------------------------
# Tests: Empty advisory map (Req 7.4)
# ---------------------------------------------------------------------------


class TestEmptyAdvisoryMap:
    """Empty advisory map when no advisory stages are enabled."""

    async def test_no_advisory_stages_produces_empty_map(self) -> None:
        """No advisory stages yields an empty advisory result map."""
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[make_passing_stage("tests")],
        )

        result = await pipeline.run()

        assert result.advisory_results == {}

    async def test_none_advisory_stages_produces_empty_map(self) -> None:
        """None advisory stages also yields an empty advisory result map."""
        pipeline = VerificationPipeline(
            advisory_stages=None,
            blocking_stages=[make_passing_stage("tests")],
        )

        result = await pipeline.run()

        assert result.advisory_results == {}


# ---------------------------------------------------------------------------
# Tests: Blocking stages run in declared order (Req 7.5)
# ---------------------------------------------------------------------------


class TestBlockingStagesDeclaredOrder:
    """Blocking stages run in declared order after advisory merge."""

    async def test_blocking_stages_run_sequentially(self) -> None:
        """Blocking stages execute in declared order."""
        execution_order: list[str] = []

        async def make_ordered_executor(name: str) -> tuple[bool, str]:
            execution_order.append(name)
            return True, f"{name} ok"

        stages = [
            VerificationStage(
                name="tests",
                timeout_s=10.0,
                executor=lambda n="tests": make_ordered_executor(n),
            ),
            VerificationStage(
                name="llm_review",
                timeout_s=10.0,
                executor=lambda n="llm_review": make_ordered_executor(n),
            ),
        ]
        pipeline = VerificationPipeline(advisory_stages=[], blocking_stages=stages)

        await pipeline.run()

        assert execution_order == ["tests", "llm_review"]

    async def test_all_blocking_pass(self) -> None:
        """All blocking stages pass — pipeline reports success."""
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[
                make_passing_stage("tests"),
                make_passing_stage("llm_review"),
            ],
        )

        result = await pipeline.run()

        assert result.all_blocking_passed
        assert result.halted_at is None
        assert len(result.blocking_results) == 2

    async def test_blocking_after_advisory(self) -> None:
        """Blocking stages run after advisory stages complete."""
        order: list[str] = []

        async def advisory_exec() -> tuple[bool, str]:
            order.append("advisory")
            await asyncio.sleep(0.02)
            return True, "ok"

        async def blocking_exec() -> tuple[bool, str]:
            order.append("blocking")
            return True, "ok"

        pipeline = VerificationPipeline(
            advisory_stages=[
                VerificationStage(name="lint", timeout_s=5.0, executor=advisory_exec),
            ],
            blocking_stages=[
                VerificationStage(name="tests", timeout_s=5.0, executor=blocking_exec),
            ],
        )

        await pipeline.run()

        assert order.index("advisory") < order.index("blocking")


# ---------------------------------------------------------------------------
# Tests: Blocking failure halts pipeline (Req 7.6)
# ---------------------------------------------------------------------------


class TestBlockingFailureHalts:
    """Blocking failure halts pipeline, retains workspace, requests policy."""

    async def test_blocking_failure_halts_pipeline(self) -> None:
        """A failed blocking stage halts the pipeline."""
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[
                make_passing_stage("tests"),
                make_failing_stage("llm_review"),
                make_passing_stage("final_check"),
            ],
            task_id="task-1",
        )

        result = await pipeline.run()

        assert not result.all_blocking_passed
        assert result.halted_at == "llm_review"
        # final_check should NOT have run
        assert "final_check" not in result.blocking_results
        assert "tests" in result.blocking_results
        assert "llm_review" in result.blocking_results

    async def test_blocking_timeout_halts_pipeline(self) -> None:
        """A timed-out blocking stage halts the pipeline."""
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[
                make_slow_stage("tests", delay=5.0, timeout_s=0.05),
                make_passing_stage("llm_review"),
            ],
            task_id="task-1",
        )

        result = await pipeline.run()

        assert not result.all_blocking_passed
        assert result.halted_at == "tests"
        assert result.blocking_results["tests"].status == StageStatus.TIMED_OUT

    async def test_blocking_failure_requests_policy_decision(self) -> None:
        """On blocking failure, pipeline produces a policy decision request."""
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[make_failing_stage("tests")],
            task_id="task-42",
        )

        result = await pipeline.run()

        assert result.policy_request is not None
        assert result.policy_request["stage"] == "tests"
        assert result.policy_request["task_id"] == "task-42"
        assert result.policy_request["action"] == "request_policy_decision"

    async def test_no_policy_request_when_all_pass(self) -> None:
        """No policy request when all blocking stages pass."""
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[make_passing_stage("tests")],
            task_id="task-1",
        )

        result = await pipeline.run()

        assert result.policy_request is None

    async def test_blocking_exception_halts_pipeline(self) -> None:
        """A blocking stage that raises an exception halts the pipeline."""
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[
                make_error_stage("tests", error="process crashed"),
                make_passing_stage("llm_review"),
            ],
            task_id="task-1",
        )

        result = await pipeline.run()

        assert not result.all_blocking_passed
        assert result.halted_at == "tests"
        assert "process crashed" in result.blocking_results["tests"].detail


# ---------------------------------------------------------------------------
# Tests: verify.stage event per stage (Req 7.7)
# ---------------------------------------------------------------------------


class TestVerifyStageEvents:
    """Emit verify.stage event per stage with status and detail."""

    async def test_advisory_stage_emits_event(self) -> None:
        """Each advisory stage emits a verify.stage event."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[
                make_passing_stage("lint"),
                make_failing_stage("type_check"),
            ],
            blocking_stages=[],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        await pipeline.run()

        stage_events = collector.events_of_type(EventType.VERIFY_STAGE)
        assert len(stage_events) == 2

        # Check event payloads
        payloads = {e.payload["stage_name"]: e.payload for e in stage_events}
        assert payloads["lint"]["status"] == "passed"
        assert payloads["type_check"]["status"] == "failed"
        assert payloads["lint"]["task_id"] == "task-1"

    async def test_blocking_stage_emits_event(self) -> None:
        """Each blocking stage emits a verify.stage event."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[
                make_passing_stage("tests"),
                make_passing_stage("llm_review"),
            ],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        await pipeline.run()

        stage_events = collector.events_of_type(EventType.VERIFY_STAGE)
        assert len(stage_events) == 2

    async def test_timed_out_stage_emits_event_with_timed_out_status(self) -> None:
        """A timed-out stage emits a verify.stage with status=timed_out."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[
                make_slow_stage("lint", delay=5.0, timeout_s=0.05),
            ],
            blocking_stages=[],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        await pipeline.run()

        stage_events = collector.events_of_type(EventType.VERIFY_STAGE)
        assert len(stage_events) == 1
        assert stage_events[0].payload["status"] == "timed_out"
        assert stage_events[0].payload["stage_name"] == "lint"

    async def test_stage_event_has_correct_source(self) -> None:
        """verify.stage events have source='verification_pipeline'."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[make_passing_stage("lint")],
            blocking_stages=[],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        await pipeline.run()

        stage_events = collector.events_of_type(EventType.VERIFY_STAGE)
        assert stage_events[0].source == "verification_pipeline"

    async def test_stage_event_has_session_id(self) -> None:
        """verify.stage events carry the correct session_id."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[make_passing_stage("lint")],
            blocking_stages=[],
            event_emitter=collector.emit,
            session_id="sess-42",
            task_id="task-1",
        )

        await pipeline.run()

        stage_events = collector.events_of_type(EventType.VERIFY_STAGE)
        assert stage_events[0].session_id == "sess-42"


# ---------------------------------------------------------------------------
# Tests: verify.passed event (Req 7.8)
# ---------------------------------------------------------------------------


class TestVerifyPassedEvent:
    """Emit verify.passed when all blocking stages pass."""

    async def test_verify_passed_emitted_when_all_blocking_pass(self) -> None:
        """verify.passed is emitted when every blocking stage passes."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[make_passing_stage("lint")],
            blocking_stages=[
                make_passing_stage("tests"),
                make_passing_stage("llm_review"),
            ],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        await pipeline.run()

        passed_events = collector.events_of_type(EventType.VERIFY_PASSED)
        assert len(passed_events) == 1
        assert passed_events[0].payload["task_id"] == "task-1"
        assert passed_events[0].session_id == "sess-1"
        assert passed_events[0].source == "verification_pipeline"

    async def test_verify_passed_not_emitted_on_blocking_failure(self) -> None:
        """verify.passed is NOT emitted when a blocking stage fails."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[make_failing_stage("tests")],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        await pipeline.run()

        passed_events = collector.events_of_type(EventType.VERIFY_PASSED)
        assert len(passed_events) == 0

    async def test_verify_passed_emitted_with_no_blocking_stages(self) -> None:
        """verify.passed is emitted when no blocking stages exist (vacuously true)."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[make_passing_stage("lint")],
            blocking_stages=[],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        result = await pipeline.run()

        # With no blocking stages, all_blocking_passed should be True
        assert result.all_blocking_passed
        passed_events = collector.events_of_type(EventType.VERIFY_PASSED)
        assert len(passed_events) == 1

    async def test_verify_passed_not_emitted_on_advisory_failure_only(self) -> None:
        """Advisory failures alone do not prevent verify.passed."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[make_failing_stage("lint")],
            blocking_stages=[make_passing_stage("tests")],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        await pipeline.run()

        # Advisory failure does not block — verify.passed should still emit
        passed_events = collector.events_of_type(EventType.VERIFY_PASSED)
        assert len(passed_events) == 1


# ---------------------------------------------------------------------------
# Tests: No event emitter configured
# ---------------------------------------------------------------------------


class TestNoEventEmitter:
    """Pipeline works correctly without an event emitter."""

    async def test_pipeline_works_without_emitter(self) -> None:
        """Pipeline runs fine when no event_emitter is configured."""
        pipeline = VerificationPipeline(
            advisory_stages=[make_passing_stage("lint")],
            blocking_stages=[make_passing_stage("tests")],
            event_emitter=None,
        )

        result = await pipeline.run()

        assert result.all_blocking_passed
        assert result.advisory_results["lint"].status == StageStatus.PASSED


# ---------------------------------------------------------------------------
# Tests: Full pipeline integration
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Integration tests for the full pipeline flow."""

    async def test_advisory_then_blocking_full_flow(self) -> None:
        """Full pipeline: advisory (mixed) then blocking (all pass)."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[
                make_passing_stage("lint"),
                make_failing_stage("type_check"),
                make_passing_stage("security"),
            ],
            blocking_stages=[
                make_passing_stage("tests"),
                make_passing_stage("llm_review"),
            ],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        result = await pipeline.run()

        # Advisory: all recorded
        assert len(result.advisory_results) == 3
        assert result.advisory_results["type_check"].status == StageStatus.FAILED

        # Blocking: all pass
        assert result.all_blocking_passed
        assert len(result.blocking_results) == 2

        # Events: 3 advisory + 2 blocking + 1 verify.passed = 6
        stage_events = collector.events_of_type(EventType.VERIFY_STAGE)
        passed_events = collector.events_of_type(EventType.VERIFY_PASSED)
        assert len(stage_events) == 5
        assert len(passed_events) == 1

    async def test_advisory_then_blocking_failure_flow(self) -> None:
        """Full pipeline: advisory then blocking failure."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[make_passing_stage("lint")],
            blocking_stages=[
                make_passing_stage("tests"),
                make_failing_stage("llm_review"),
                make_passing_stage("final_gate"),
            ],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        result = await pipeline.run()

        # Advisory: passes
        assert result.advisory_results["lint"].status == StageStatus.PASSED

        # Blocking: halts at llm_review
        assert not result.all_blocking_passed
        assert result.halted_at == "llm_review"
        assert "final_gate" not in result.blocking_results

        # Events: 1 advisory + 2 blocking (tests + llm_review) = 3 stage events, no passed
        stage_events = collector.events_of_type(EventType.VERIFY_STAGE)
        passed_events = collector.events_of_type(EventType.VERIFY_PASSED)
        assert len(stage_events) == 3
        assert len(passed_events) == 0

    async def test_empty_pipeline(self) -> None:
        """A pipeline with no stages still produces a valid result."""
        collector = FakeEventCollector()
        pipeline = VerificationPipeline(
            advisory_stages=[],
            blocking_stages=[],
            event_emitter=collector.emit,
            session_id="sess-1",
            task_id="task-1",
        )

        result = await pipeline.run()

        assert result.advisory_results == {}
        assert result.blocking_results == {}
        assert result.all_blocking_passed
        # verify.passed is emitted even with no stages
        passed_events = collector.events_of_type(EventType.VERIFY_PASSED)
        assert len(passed_events) == 1
