"""Property-based tests for plan acyclicity using Hypothesis.

**Validates: Requirements 5.1, 5.2, 5.3, 5.4**

Property 5: Plan acyclicity — for all task sets, `plan` produces a DAG or
    reports a cycle; every `depends_on` references an existing task id.

For randomly generated task sets (with and without cycles, with and without
unresolved dependencies), the planner must:
1. When returning a topological ordering: every task appears after all its deps
2. When reporting a cycle: the reported IDs actually form a cycle
3. When reporting an unresolved dependency: the referenced ID is genuinely not
   in the task set
"""

from __future__ import annotations

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.runtime.planner import PlanErrorKind, PlanResult, plan


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Task IDs: short identifiers for readability in counterexamples
task_id_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=6,
)


@st.composite
def task_set_acyclic(draw: st.DrawFn) -> list[dict[str, object]]:
    """Generate a task set that is guaranteed to be acyclic (valid DAG).

    Strategy: generate task IDs, then only allow dependencies from earlier
    tasks to later tasks in the generated order (topological by construction).
    """
    num_tasks = draw(st.integers(min_value=1, max_value=10))
    ids = draw(
        st.lists(task_id_strategy, min_size=num_tasks, max_size=num_tasks, unique=True)
    )

    tasks: list[dict[str, object]] = []
    for i, task_id in enumerate(ids):
        # Only depend on tasks that appear earlier in the list (guaranteed acyclic)
        possible_deps = ids[:i]
        if possible_deps:
            deps = draw(
                st.lists(
                    st.sampled_from(possible_deps),
                    min_size=0,
                    max_size=min(3, len(possible_deps)),
                    unique=True,
                )
            )
        else:
            deps = []
        tasks.append({"id": task_id, "depends_on": deps})

    return tasks


@st.composite
def task_set_with_cycle(draw: st.DrawFn) -> list[dict[str, object]]:
    """Generate a task set that is guaranteed to contain at least one cycle.

    Strategy: generate unique task IDs, then create a guaranteed cycle by
    making a chain A -> B -> ... -> A (where -> means "depends on").
    Additional random edges are added for variety.
    """
    num_tasks = draw(st.integers(min_value=2, max_value=10))
    ids = draw(
        st.lists(task_id_strategy, min_size=num_tasks, max_size=num_tasks, unique=True)
    )

    # Pick a subset of at least 2 tasks to form the cycle
    cycle_size = draw(st.integers(min_value=2, max_value=num_tasks))
    cycle_indices = draw(
        st.lists(
            st.integers(min_value=0, max_value=num_tasks - 1),
            min_size=cycle_size,
            max_size=cycle_size,
            unique=True,
        )
    )
    cycle_ids = [ids[i] for i in cycle_indices]

    # Build a dependency map: start with cycle edges
    # cycle_ids[0] depends on cycle_ids[1], cycle_ids[1] depends on cycle_ids[2], ...
    # cycle_ids[-1] depends on cycle_ids[0] (closing the loop)
    dep_map: dict[str, list[str]] = {tid: [] for tid in ids}
    for i in range(len(cycle_ids)):
        task_id = cycle_ids[i]
        dep_id = cycle_ids[(i + 1) % len(cycle_ids)]
        if dep_id not in dep_map[task_id]:
            dep_map[task_id].append(dep_id)

    # Optionally add a few more random edges (may or may not create more cycles)
    for task_id in ids:
        other_ids = [tid for tid in ids if tid != task_id and tid not in dep_map[task_id]]
        if other_ids:
            extra_deps = draw(
                st.lists(
                    st.sampled_from(other_ids),
                    min_size=0,
                    max_size=min(2, len(other_ids)),
                    unique=True,
                )
            )
            dep_map[task_id].extend(extra_deps)

    tasks = [{"id": tid, "depends_on": dep_map[tid]} for tid in ids]
    return tasks


@st.composite
def task_set_with_unresolved(draw: st.DrawFn) -> list[dict[str, object]]:
    """Generate a task set with at least one unresolved dependency.

    Strategy: generate valid tasks, then add a dependency referencing an
    ID that does not exist in the task set.
    """
    num_tasks = draw(st.integers(min_value=1, max_value=10))
    ids = draw(
        st.lists(task_id_strategy, min_size=num_tasks, max_size=num_tasks, unique=True)
    )

    tasks: list[dict[str, object]] = []
    for i, task_id in enumerate(ids):
        possible_deps = ids[:i]
        if possible_deps:
            deps = draw(
                st.lists(
                    st.sampled_from(possible_deps),
                    min_size=0,
                    max_size=min(2, len(possible_deps)),
                    unique=True,
                )
            )
        else:
            deps = []
        tasks.append({"id": task_id, "depends_on": deps})

    # Generate a non-existent ID and inject it as a dependency
    fake_id = draw(task_id_strategy.filter(lambda x: x not in ids))
    target_idx = draw(st.integers(min_value=0, max_value=num_tasks - 1))
    target_task = tasks[target_idx]
    deps_list = list(target_task["depends_on"])  # type: ignore[arg-type]
    deps_list.append(fake_id)
    tasks[target_idx] = {"id": target_task["id"], "depends_on": deps_list}

    return tasks


@st.composite
def arbitrary_task_set(draw: st.DrawFn) -> list[dict[str, object]]:
    """Generate a completely arbitrary task set with random dependencies.

    Tasks may have:
    - Valid dependencies (existing task IDs)
    - Cycles (back edges)
    - Unresolved dependencies (non-existent IDs)
    - No dependencies at all
    """
    num_tasks = draw(st.integers(min_value=1, max_value=10))
    ids = draw(
        st.lists(task_id_strategy, min_size=num_tasks, max_size=num_tasks, unique=True)
    )

    # Occasionally add some non-existent IDs to the dependency pool
    extra_ids = draw(
        st.lists(
            task_id_strategy.filter(lambda x: x not in ids),
            min_size=0,
            max_size=3,
        )
    )
    all_possible_deps = ids + extra_ids

    tasks: list[dict[str, object]] = []
    for task_id in ids:
        # Allow depending on any ID (including self, others, or non-existent)
        other_deps = [d for d in all_possible_deps if d != task_id]
        if other_deps:
            deps = draw(
                st.lists(
                    st.sampled_from(other_deps),
                    min_size=0,
                    max_size=min(4, len(other_deps)),
                    unique=True,
                )
            )
        else:
            deps = []
        tasks.append({"id": task_id, "depends_on": deps})

    return tasks


# ---------------------------------------------------------------------------
# Property 5: Plan Acyclicity
# ---------------------------------------------------------------------------


class TestPlanAcyclicityProperty:
    """Property 5: Plan acyclicity — for all task sets, `plan` produces a DAG
    or reports a cycle; every `depends_on` references an existing task id.

    **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
    """

    @given(tasks=task_set_acyclic())
    @settings(max_examples=200, deadline=5000)
    def test_acyclic_input_produces_valid_topological_ordering(
        self,
        tasks: list[dict[str, object]],
    ) -> None:
        """Req 5.2: When the dependency graph contains no cycles, produce a
        topological ordering where every task appears only after all tasks
        it depends on.

        For any acyclic task set (constructed to be cycle-free), the planner
        must return a valid topological ordering.
        """
        result = plan(tasks)  # type: ignore[arg-type]

        # Must succeed (no errors)
        assert result.is_ok, (
            f"Planner reported errors for a known-acyclic task set: "
            f"{result.errors}"
        )

        # All task IDs must appear exactly once in the ordering
        task_ids = {t["id"] for t in tasks}
        assert set(result.ordering) == task_ids, (
            f"Ordering does not contain all task IDs. "
            f"Expected: {sorted(task_ids)}, Got: {sorted(result.ordering)}"
        )
        assert len(result.ordering) == len(task_ids), (
            f"Ordering has duplicates or wrong length. "
            f"Expected {len(task_ids)} items, got {len(result.ordering)}"
        )

        # Topological validity: every task appears AFTER all its dependencies
        position = {tid: idx for idx, tid in enumerate(result.ordering)}
        for task in tasks:
            task_id = task["id"]
            for dep_id in task["depends_on"]:  # type: ignore[union-attr]
                assert position[dep_id] < position[task_id], (  # type: ignore[index]
                    f"Topological violation: task '{task_id}' depends on "
                    f"'{dep_id}', but '{dep_id}' appears at position "
                    f"{position[dep_id]} which is not before '{task_id}' "  # type: ignore[index]
                    f"at position {position[task_id]}"
                )

    @given(tasks=task_set_with_cycle())
    @settings(max_examples=200, deadline=5000)
    def test_cyclic_input_reports_cycle_error(
        self,
        tasks: list[dict[str, object]],
    ) -> None:
        """Req 5.3: If task dependencies contain a cycle, produce an error
        that identifies the set of task identifiers forming the detected cycle.

        For any task set with an injected cycle, the planner must report a
        cycle error (not succeed or report a different error).
        """
        result = plan(tasks)  # type: ignore[arg-type]

        # Must fail (not produce an ordering)
        assert not result.is_ok, (
            "Planner produced a valid ordering for a task set that contains "
            "a cycle. Expected a cycle error."
        )

        # At least one error must be CYCLE_DETECTED
        cycle_errors = [
            e for e in result.errors
            if e.kind == PlanErrorKind.CYCLE_DETECTED
        ]
        assert len(cycle_errors) >= 1, (
            f"Expected at least one CYCLE_DETECTED error but got: "
            f"{[e.kind for e in result.errors]}"
        )

        # The reported cycle task IDs must all be valid task IDs in the set
        all_task_ids = {t["id"] for t in tasks}
        for error in cycle_errors:
            for tid in error.task_ids:
                assert tid in all_task_ids, (
                    f"Cycle error references task '{tid}' which is not in "
                    f"the task set. Cycle IDs must be genuine task identifiers."
                )

            # The reported cycle must have at least 2 distinct nodes
            # (a cycle needs at least 2 tasks to form a loop)
            distinct_ids = set(error.task_ids)
            assert len(distinct_ids) >= 2, (
                f"Reported cycle has fewer than 2 distinct task IDs: "
                f"{error.task_ids}. A cycle requires at least 2 tasks."
            )

    @given(tasks=task_set_with_unresolved())
    @settings(max_examples=200, deadline=5000)
    def test_unresolved_dependency_reports_error(
        self,
        tasks: list[dict[str, object]],
    ) -> None:
        """Req 5.4: If a task dependency references a task identifier that
        does not exist, produce an error identifying the referencing task
        and the unresolved dependency.

        For any task set with an injected non-existent dependency, the planner
        must report an unresolved dependency error.
        """
        result = plan(tasks)  # type: ignore[arg-type]

        # Must fail
        assert not result.is_ok, (
            "Planner produced a valid ordering for a task set with an "
            "unresolved dependency. Expected an error."
        )

        # At least one error must be UNRESOLVED_DEPENDENCY
        unresolved_errors = [
            e for e in result.errors
            if e.kind == PlanErrorKind.UNRESOLVED_DEPENDENCY
        ]
        assert len(unresolved_errors) >= 1, (
            f"Expected at least one UNRESOLVED_DEPENDENCY error but got: "
            f"{[e.kind for e in result.errors]}"
        )

        # For each unresolved error, the missing dep ID must genuinely not
        # be in the task set
        all_task_ids = {t["id"] for t in tasks}
        for error in unresolved_errors:
            assert len(error.task_ids) == 2, (
                f"UNRESOLVED_DEPENDENCY error should have exactly 2 task_ids "
                f"[referencing_task, missing_dep], got: {error.task_ids}"
            )
            referencing_id = error.task_ids[0]
            missing_id = error.task_ids[1]

            # The referencing task must exist in the task set
            assert referencing_id in all_task_ids, (
                f"Referencing task '{referencing_id}' in unresolved error "
                f"is not in the task set."
            )

            # The missing dependency must NOT exist in the task set
            assert missing_id not in all_task_ids, (
                f"Supposedly unresolved dependency '{missing_id}' actually "
                f"exists in the task set. The error is incorrect."
            )

    @given(tasks=arbitrary_task_set())
    @settings(max_examples=200, deadline=5000)
    def test_plan_always_returns_valid_result(
        self,
        tasks: list[dict[str, object]],
    ) -> None:
        """For any arbitrary task set, the planner must either produce a valid
        ordering (success) or report at least one error (failure). It must
        never crash, and when successful, the ordering must be a valid
        topological sort.

        This is the main property: plan always produces a well-formed result.
        """
        result = plan(tasks)  # type: ignore[arg-type]

        all_task_ids = {t["id"] for t in tasks}

        if result.is_ok:
            # Success case: valid topological ordering
            assert len(result.ordering) == len(all_task_ids), (
                f"Ordering length {len(result.ordering)} != "
                f"task count {len(all_task_ids)}"
            )
            assert set(result.ordering) == all_task_ids

            # Topological validity check
            position = {tid: idx for idx, tid in enumerate(result.ordering)}
            for task in tasks:
                task_id = task["id"]
                for dep_id in task["depends_on"]:  # type: ignore[union-attr]
                    assert dep_id in all_task_ids, (  # type: ignore[operator]
                        f"Dependency '{dep_id}' is not in the task set but "
                        f"planner did not report it as unresolved."
                    )
                    assert position[dep_id] < position[task_id], (  # type: ignore[index]
                        f"Topological violation in ordering: '{task_id}' "
                        f"depends on '{dep_id}' but '{dep_id}' comes after."
                    )
        else:
            # Failure case: at least one well-formed error
            assert len(result.errors) >= 1, (
                "Result is not OK but has no errors."
            )
            assert result.ordering == [], (
                "Failed result should have an empty ordering."
            )

            # Verify error kinds are valid
            for error in result.errors:
                assert error.kind in (
                    PlanErrorKind.CYCLE_DETECTED,
                    PlanErrorKind.UNRESOLVED_DEPENDENCY,
                ), f"Unknown error kind: {error.kind}"

    @given(tasks=task_set_acyclic())
    @settings(max_examples=100, deadline=5000)
    def test_all_dependencies_reference_existing_tasks(
        self,
        tasks: list[dict[str, object]],
    ) -> None:
        """Req 5.1, 5.4: In any successful plan result, every depends_on
        reference must point to an existing task ID in the task set.

        This verifies the DAG construction invariant: edges only connect
        existing nodes.
        """
        result = plan(tasks)  # type: ignore[arg-type]

        # For acyclic input with no unresolved deps, plan must succeed
        assert result.is_ok

        # Every dependency in the original task set must be a real task ID
        all_task_ids = {t["id"] for t in tasks}
        for task in tasks:
            for dep_id in task["depends_on"]:  # type: ignore[union-attr]
                assert dep_id in all_task_ids, (  # type: ignore[operator]
                    f"Task '{task['id']}' depends on '{dep_id}' which is "
                    f"not in the task set. Planner should have caught this."
                )
