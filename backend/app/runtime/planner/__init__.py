"""Task dependency planning with DAG construction.

Implements the planning phase of the build workflow: constructs a directed
dependency graph from a task list, validates that all dependencies are resolved,
checks for cycles, and produces a topological ordering when the graph is valid.

Requirements: 5.1, 5.2, 5.3, 5.4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PlanErrorKind(str, Enum):
    """Kinds of planning errors."""

    CYCLE_DETECTED = "cycle_detected"
    UNRESOLVED_DEPENDENCY = "unresolved_dependency"


@dataclass(frozen=True)
class PlanError:
    """An error produced during dependency planning.

    Attributes:
        kind: The type of error (cycle or unresolved dependency).
        task_ids: Task identifiers involved in the error.
            For cycles: the task IDs forming the cycle.
            For unresolved deps: [referencing_task_id, missing_dep_id].
        message: Human-readable description of the error.
    """

    kind: PlanErrorKind
    task_ids: list[str] = field(default_factory=list)
    message: str = ""


@dataclass(frozen=True)
class PlanResult:
    """Result of the dependency planning operation.

    Either contains a valid topological ordering (success) or one or more
    errors (failure). Check `is_ok` to determine which case applies.

    Attributes:
        ordering: The topological ordering of task IDs (empty on failure).
        errors: List of planning errors (empty on success).
    """

    ordering: list[str] = field(default_factory=list)
    errors: list[PlanError] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        """True when planning succeeded (no errors, valid ordering)."""
        return len(self.errors) == 0


def plan(tasks: list[dict[str, Any]]) -> PlanResult:
    """Construct a dependency DAG and produce a topological ordering.

    Accepts a task list (list of dicts with at minimum 'id' and 'depends_on'
    fields) and:
    1. Validates all dependency references exist (Req 5.4)
    2. Builds a directed graph where edges represent dependencies
       (task A depends on task B → edge B → A) (Req 5.1)
    3. Checks for cycles using DFS (Req 5.3)
    4. If valid: produces a topological ordering (Req 5.2)

    Args:
        tasks: A list of task dicts. Each must have:
            - 'id': str — unique task identifier
            - 'depends_on': list[str] — IDs of tasks this task depends on

    Returns:
        A PlanResult with either a valid ordering or error(s).
    """
    # Collect all task IDs
    task_ids: set[str] = set()
    for task in tasks:
        task_ids.add(task["id"])

    # Step 1: Check for unresolved dependencies (Req 5.4)
    unresolved_errors: list[PlanError] = []
    for task in tasks:
        task_id = task["id"]
        depends_on: list[str] = task.get("depends_on", []) or []
        for dep_id in depends_on:
            if dep_id not in task_ids:
                unresolved_errors.append(
                    PlanError(
                        kind=PlanErrorKind.UNRESOLVED_DEPENDENCY,
                        task_ids=[task_id, dep_id],
                        message=(
                            f"Task '{task_id}' depends on '{dep_id}' "
                            f"which does not exist in the task set"
                        ),
                    )
                )

    if unresolved_errors:
        return PlanResult(errors=unresolved_errors)

    # Step 2: Build adjacency list (Req 5.1)
    # Edge direction: if A depends on B, then B → A (B must come before A)
    # We store: graph[node] = list of nodes that depend on this node (successors)
    # and in_degree[node] = number of dependencies this node has (predecessors)
    graph: dict[str, list[str]] = {task["id"]: [] for task in tasks}
    in_degree: dict[str, int] = {task["id"]: 0 for task in tasks}

    for task in tasks:
        task_id = task["id"]
        depends_on = task.get("depends_on", []) or []
        for dep_id in depends_on:
            graph[dep_id].append(task_id)
            in_degree[task_id] += 1

    # Step 3: Detect cycles using DFS (Req 5.3)
    # We use a coloring approach: WHITE=unvisited, GRAY=in-stack, BLACK=done
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in task_ids}
    parent: dict[str, str | None] = {tid: None for tid in task_ids}
    cycle_found: list[str] | None = None

    def dfs(node: str) -> bool:
        """DFS visit; returns True if a cycle is found."""
        nonlocal cycle_found
        color[node] = GRAY
        for neighbor in graph[node]:
            if color[neighbor] == GRAY:
                # Found a back edge → cycle
                # Reconstruct the cycle path
                cycle = _extract_cycle(node, neighbor, parent, graph)
                cycle_found = cycle
                return True
            if color[neighbor] == WHITE:
                parent[neighbor] = node
                if dfs(neighbor):
                    return True
        color[node] = BLACK
        return False

    for tid in sorted(task_ids):  # sorted for determinism
        if color[tid] == WHITE:
            if dfs(tid):
                break

    if cycle_found is not None:
        return PlanResult(
            errors=[
                PlanError(
                    kind=PlanErrorKind.CYCLE_DETECTED,
                    task_ids=cycle_found,
                    message=(
                        f"Cycle detected among tasks: "
                        f"{' -> '.join(cycle_found)}"
                    ),
                )
            ]
        )

    # Step 4: Topological sort using Kahn's algorithm (Req 5.2)
    # This guarantees that every task appears after all its dependencies.
    ordering: list[str] = []
    queue: list[str] = sorted(
        [tid for tid, deg in in_degree.items() if deg == 0]
    )

    while queue:
        # Pop from the front (stable ordering for determinism)
        node = queue.pop(0)
        ordering.append(node)
        for neighbor in sorted(graph[node]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
        queue.sort()  # maintain sorted order for determinism

    return PlanResult(ordering=ordering)


def _extract_cycle(
    back_edge_from: str,
    back_edge_to: str,
    parent: dict[str, str | None],
    graph: dict[str, list[str]],
) -> list[str]:
    """Extract the cycle path from a detected back edge.

    Traces back from `back_edge_from` to `back_edge_to` through the parent
    chain to reconstruct the set of task IDs forming the cycle.

    Args:
        back_edge_from: The node where the back edge originates.
        back_edge_to: The node where the back edge points (ancestor in DFS).
        parent: The DFS parent map.
        graph: The adjacency list.

    Returns:
        A list of task IDs that form the cycle.
    """
    # Trace from back_edge_from back to back_edge_to via parents
    cycle: list[str] = [back_edge_to]
    current = back_edge_from
    while current != back_edge_to:
        cycle.append(current)
        p = parent.get(current)
        if p is None:
            break
        current = p
    cycle.append(back_edge_to)  # close the cycle
    cycle.reverse()
    return cycle
