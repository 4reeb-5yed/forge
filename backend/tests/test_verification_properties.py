"""Property-based tests for verification merge order-independence using Hypothesis.

**Validates: Requirements 7.3, 7.4**

Property 3: Verification merge order-independence — for all sets of advisory
    verifier results, the merged dict[stage_name -> result] is independent of
    wall-clock arrival order.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from typing import Any, Awaitable, Callable

import pytest
from hypothesis import given, settings, strategies as st, assume

from app.runtime.verification import (
    PipelineResult,
    StageResult,
    StageStatus,
    VerificationPipeline,
    VerificationStage,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy for generating valid stage names (unique identifiers)
stage_name_st = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")

# Strategy for generating stage statuses
stage_status_st = st.sampled_from([True, False])

# Strategy for detail strings
detail_st = st.text(min_size=0, max_size=100)

# Strategy for generating a delay in seconds (simulating wall-clock variation)
delay_st = st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False)


# Strategy for generating a set of advisory stage configurations
def advisory_stage_configs_st(min_stages: int = 1, max_stages: int = 8):
    """Generate a list of unique stage configs: (name, passes, detail, delay)."""
    return st.lists(
        st.tuples(stage_name_st, stage_status_st, detail_st, delay_st),
        min_size=min_stages,
        max_size=max_stages,
        unique_by=lambda t: t[0],  # unique names
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_advisory_stage(
    name: str, passes: bool, detail: str, delay: float
) -> VerificationStage:
    """Create an advisory VerificationStage with a configurable delay."""

    async def executor() -> tuple[bool, str]:
        if delay > 0:
            await asyncio.sleep(delay)
        return (passes, detail)

    return VerificationStage(name=name, timeout_s=5.0, executor=executor)


async def run_pipeline_with_stages(
    stage_configs: list[tuple[str, bool, str, float]],
) -> dict[str, StageResult]:
    """Run a VerificationPipeline with the given advisory stage configs and return advisory_results."""
    stages = [
        make_advisory_stage(name, passes, detail, delay)
        for name, passes, detail, delay in stage_configs
    ]
    pipeline = VerificationPipeline(
        advisory_stages=stages,
        blocking_stages=[],
        session_id="test-session",
        task_id="test-task",
    )
    result = await pipeline.run()
    return result.advisory_results


# ---------------------------------------------------------------------------
# Property 3: Verification merge order-independence
# ---------------------------------------------------------------------------


class TestVerificationMergeOrderIndependence:
    """Property 3: Verification merge order-independence — for all sets of
    advisory verifier results, the merged dict[stage_name -> result] is
    independent of wall-clock arrival order.

    **Validates: Requirements 7.3, 7.4**
    """

    @given(stage_configs=advisory_stage_configs_st(min_stages=1, max_stages=8))
    @settings(max_examples=200, deadline=10000)
    def test_merged_results_independent_of_completion_order(
        self, stage_configs: list[tuple[str, bool, str, float]]
    ) -> None:
        """For any set of advisory stages, running them with different
        completion orders (by varying delays) yields the same merged result
        dict, keyed by stage name.

        We run the same logical stages twice:
        - Once with the original delays
        - Once with shuffled/reversed delays

        The resulting advisory_results dicts must be identical.
        """
        assume(len(stage_configs) >= 2)

        # Run 1: original delay ordering
        results_1 = asyncio.run(
            run_pipeline_with_stages(stage_configs)
        )

        # Run 2: reverse the delays to change wall-clock arrival order
        reversed_configs = [
            (name, passes, detail, max_delay - delay)
            for (name, passes, detail, delay), max_delay in zip(
                stage_configs,
                [max(d for _, _, _, d in stage_configs)] * len(stage_configs),
            )
        ]
        results_2 = asyncio.run(
            run_pipeline_with_stages(reversed_configs)
        )

        # The merged dicts must be identical (same keys, same values)
        assert results_1.keys() == results_2.keys(), (
            f"Keys differ: {results_1.keys()} vs {results_2.keys()}"
        )
        for stage_name in results_1:
            r1 = results_1[stage_name]
            r2 = results_2[stage_name]
            assert r1.name == r2.name, (
                f"Stage '{stage_name}' name mismatch: {r1.name} vs {r2.name}"
            )
            assert r1.status == r2.status, (
                f"Stage '{stage_name}' status mismatch: {r1.status} vs {r2.status}"
            )
            assert r1.detail == r2.detail, (
                f"Stage '{stage_name}' detail mismatch: {r1.detail!r} vs {r2.detail!r}"
            )

    @given(stage_configs=advisory_stage_configs_st(min_stages=2, max_stages=8))
    @settings(max_examples=150, deadline=10000)
    def test_merged_keys_exactly_match_input_stage_names(
        self, stage_configs: list[tuple[str, bool, str, float]]
    ) -> None:
        """The merged advisory results dict keys must exactly match the input
        stage names — no loss, no extras.

        This verifies completeness: every configured stage appears in the result.
        """
        expected_names = {name for name, _, _, _ in stage_configs}

        results = asyncio.run(
            run_pipeline_with_stages(stage_configs)
        )

        actual_names = set(results.keys())
        assert actual_names == expected_names, (
            f"Result keys do not match input stage names.\n"
            f"  Missing: {expected_names - actual_names}\n"
            f"  Extra: {actual_names - expected_names}"
        )

    @given(
        stage_configs=advisory_stage_configs_st(min_stages=2, max_stages=6),
        data=st.data(),
    )
    @settings(max_examples=100, deadline=15000)
    def test_permuted_stage_order_yields_same_results(
        self, stage_configs: list[tuple[str, bool, str, float]], data: st.DataObject
    ) -> None:
        """Running the same set of stages in different input orders (permuted
        list) with uniform zero delay yields identical merged results.

        This tests that the pipeline's internal ordering (asyncio.gather) does
        not leak into the result map.
        """
        assume(len(stage_configs) >= 2)

        # All stages with zero delay to let asyncio scheduling determine order
        zero_delay_configs = [
            (name, passes, detail, 0.0) for name, passes, detail, _ in stage_configs
        ]

        # Run with original order
        results_original = asyncio.run(
            run_pipeline_with_stages(zero_delay_configs)
        )

        # Draw a permutation of the configs
        permuted = data.draw(
            st.permutations(zero_delay_configs), label="permuted_configs"
        )

        # Run with permuted order
        results_permuted = asyncio.run(
            run_pipeline_with_stages(permuted)
        )

        # Results must be identical
        assert results_original.keys() == results_permuted.keys(), (
            f"Keys differ after permutation: "
            f"{results_original.keys()} vs {results_permuted.keys()}"
        )
        for stage_name in results_original:
            r1 = results_original[stage_name]
            r2 = results_permuted[stage_name]
            assert r1.name == r2.name
            assert r1.status == r2.status
            assert r1.detail == r2.detail

    @given(stage_configs=advisory_stage_configs_st(min_stages=0, max_stages=0))
    @settings(max_examples=50, deadline=5000)
    def test_empty_advisory_stages_produce_empty_map(
        self, stage_configs: list[tuple[str, bool, str, float]]
    ) -> None:
        """When no advisory stages are enabled, the pipeline produces an empty
        advisory result map (Requirement 7.4).
        """
        results = asyncio.run(
            run_pipeline_with_stages(stage_configs)
        )
        assert results == {}, f"Expected empty dict, got: {results}"
