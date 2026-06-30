"""Unit tests for the task dependency planner.

Tests DAG construction, topological ordering, cycle detection,
and unresolved dependency detection.

Requirements: 5.1, 5.2, 5.3, 5.4
"""

from app.runtime.planner import PlanError, PlanErrorKind, PlanResult, plan


class TestPlanSuccess:
    """Tests for valid (acyclic, resolved) task graphs."""

    def test_single_task_no_dependencies(self) -> None:
        """A single task with no dependencies produces a trivial ordering."""
        tasks = [{"id": "t1", "title": "Task 1", "depends_on": []}]
        result = plan(tasks)

        assert result.is_ok
        assert result.ordering == ["t1"]
        assert result.errors == []

    def test_linear_chain(self) -> None:
        """A linear dependency chain produces the correct order."""
        tasks = [
            {"id": "t3", "title": "Task 3", "depends_on": ["t2"]},
            {"id": "t1", "title": "Task 1", "depends_on": []},
            {"id": "t2", "title": "Task 2", "depends_on": ["t1"]},
        ]
        result = plan(tasks)

        assert result.is_ok
        # t1 must come before t2, t2 must come before t3
        assert result.ordering.index("t1") < result.ordering.index("t2")
        assert result.ordering.index("t2") < result.ordering.index("t3")

    def test_diamond_dependency(self) -> None:
        """A diamond graph (A -> B, A -> C, B -> D, C -> D) is valid."""
        tasks = [
            {"id": "A", "title": "A", "depends_on": []},
            {"id": "B", "title": "B", "depends_on": ["A"]},
            {"id": "C", "title": "C", "depends_on": ["A"]},
            {"id": "D", "title": "D", "depends_on": ["B", "C"]},
        ]
        result = plan(tasks)

        assert result.is_ok
        assert result.ordering.index("A") < result.ordering.index("B")
        assert result.ordering.index("A") < result.ordering.index("C")
        assert result.ordering.index("B") < result.ordering.index("D")
        assert result.ordering.index("C") < result.ordering.index("D")

    def test_multiple_independent_tasks(self) -> None:
        """Tasks with no dependencies can appear in any order."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": []},
            {"id": "t2", "title": "Task 2", "depends_on": []},
            {"id": "t3", "title": "Task 3", "depends_on": []},
        ]
        result = plan(tasks)

        assert result.is_ok
        assert set(result.ordering) == {"t1", "t2", "t3"}
        assert len(result.ordering) == 3

    def test_empty_task_list(self) -> None:
        """An empty task list produces an empty ordering."""
        result = plan([])

        assert result.is_ok
        assert result.ordering == []
        assert result.errors == []

    def test_complex_dag(self) -> None:
        """A complex DAG with multiple paths produces valid ordering."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": []},
            {"id": "t2", "title": "Task 2", "depends_on": ["t1"]},
            {"id": "t3", "title": "Task 3", "depends_on": ["t1"]},
            {"id": "t4", "title": "Task 4", "depends_on": ["t2", "t3"]},
            {"id": "t5", "title": "Task 5", "depends_on": ["t3"]},
            {"id": "t6", "title": "Task 6", "depends_on": ["t4", "t5"]},
        ]
        result = plan(tasks)

        assert result.is_ok
        assert len(result.ordering) == 6
        # Verify dependency constraints
        for task in tasks:
            for dep in task["depends_on"]:
                assert result.ordering.index(dep) < result.ordering.index(task["id"])

    def test_depends_on_none_treated_as_empty(self) -> None:
        """A task with depends_on=None is treated as no dependencies."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": None},
            {"id": "t2", "title": "Task 2", "depends_on": ["t1"]},
        ]
        result = plan(tasks)

        assert result.is_ok
        assert result.ordering == ["t1", "t2"]

    def test_ordering_is_deterministic(self) -> None:
        """Same input produces the same ordering on repeated calls."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": []},
            {"id": "t2", "title": "Task 2", "depends_on": []},
            {"id": "t3", "title": "Task 3", "depends_on": ["t1", "t2"]},
        ]
        results = [plan(tasks) for _ in range(10)]
        assert all(r.ordering == results[0].ordering for r in results)


class TestCycleDetection:
    """Tests for cycle detection (Req 5.3)."""

    def test_simple_cycle_two_tasks(self) -> None:
        """A mutual dependency between two tasks is detected as a cycle."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["t2"]},
            {"id": "t2", "title": "Task 2", "depends_on": ["t1"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.kind == PlanErrorKind.CYCLE_DETECTED
        # Both tasks should appear in the cycle
        assert "t1" in error.task_ids
        assert "t2" in error.task_ids

    def test_self_dependency(self) -> None:
        """A task depending on itself is detected as a cycle."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["t1"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.kind == PlanErrorKind.CYCLE_DETECTED
        assert "t1" in error.task_ids

    def test_three_task_cycle(self) -> None:
        """A cycle across three tasks is detected."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["t3"]},
            {"id": "t2", "title": "Task 2", "depends_on": ["t1"]},
            {"id": "t3", "title": "Task 3", "depends_on": ["t2"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.kind == PlanErrorKind.CYCLE_DETECTED
        # All three should be in the cycle
        assert "t1" in error.task_ids
        assert "t2" in error.task_ids
        assert "t3" in error.task_ids

    def test_cycle_does_not_produce_ordering(self) -> None:
        """When a cycle is detected, no ordering is produced (Req 5.3)."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["t2"]},
            {"id": "t2", "title": "Task 2", "depends_on": ["t1"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert result.ordering == []

    def test_cycle_in_subgraph_with_valid_nodes(self) -> None:
        """A cycle in part of the graph is detected even with valid nodes."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": []},
            {"id": "t2", "title": "Task 2", "depends_on": ["t3"]},
            {"id": "t3", "title": "Task 3", "depends_on": ["t2"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        error = result.errors[0]
        assert error.kind == PlanErrorKind.CYCLE_DETECTED
        assert "t2" in error.task_ids
        assert "t3" in error.task_ids


class TestUnresolvedDependencies:
    """Tests for unresolved dependency detection (Req 5.4)."""

    def test_single_unresolved_dependency(self) -> None:
        """A reference to a non-existent task is reported."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["missing_task"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.kind == PlanErrorKind.UNRESOLVED_DEPENDENCY
        assert error.task_ids == ["t1", "missing_task"]

    def test_multiple_unresolved_dependencies(self) -> None:
        """Multiple unresolved references each produce their own error."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["missing_a"]},
            {"id": "t2", "title": "Task 2", "depends_on": ["missing_b"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert len(result.errors) == 2
        assert all(
            e.kind == PlanErrorKind.UNRESOLVED_DEPENDENCY for e in result.errors
        )

    def test_unresolved_does_not_produce_ordering(self) -> None:
        """When unresolved deps exist, no ordering is produced (Req 5.4)."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["ghost"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert result.ordering == []

    def test_one_task_with_multiple_missing_deps(self) -> None:
        """A task referencing multiple missing deps reports each one."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["x", "y"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert len(result.errors) == 2
        dep_pairs = [(e.task_ids[0], e.task_ids[1]) for e in result.errors]
        assert ("t1", "x") in dep_pairs
        assert ("t1", "y") in dep_pairs

    def test_unresolved_reported_before_cycle_check(self) -> None:
        """Unresolved dependencies are reported without attempting cycle check."""
        # Even if there's also a cycle, unresolved deps are caught first
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["t2", "missing"]},
            {"id": "t2", "title": "Task 2", "depends_on": ["t1"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        # Should report the unresolved dependency
        assert any(
            e.kind == PlanErrorKind.UNRESOLVED_DEPENDENCY for e in result.errors
        )


class TestPlanResultInterface:
    """Tests for the PlanResult dataclass interface."""

    def test_is_ok_true_on_success(self) -> None:
        """is_ok returns True when there are no errors."""
        result = PlanResult(ordering=["t1", "t2"])
        assert result.is_ok

    def test_is_ok_false_on_errors(self) -> None:
        """is_ok returns False when there are errors."""
        result = PlanResult(
            errors=[
                PlanError(
                    kind=PlanErrorKind.CYCLE_DETECTED,
                    task_ids=["t1", "t2"],
                )
            ]
        )
        assert not result.is_ok

    def test_error_message_contains_task_ids(self) -> None:
        """Error messages contain the relevant task identifiers."""
        tasks = [
            {"id": "t1", "title": "Task 1", "depends_on": ["nonexistent"]},
        ]
        result = plan(tasks)

        assert not result.is_ok
        assert "t1" in result.errors[0].message
        assert "nonexistent" in result.errors[0].message
