"""Tests for workflow infrastructure: routing, bootstrap, assemble_deps, and app.

Covers:
- Routing functions return correct node names for various states
- assemble_deps returns a RuntimeDeps with all fields populated
- bootstrap doesn't crash with in-memory stores
- create_app returns a FastAPI instance
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime.models import ForgeState
from app.workflow.routing import (
    _END,
    route_after_classify,
    route_after_commit,
    route_after_policy,
    route_after_verify,
)


# ---------------------------------------------------------------------------
# Tests: Routing functions
# ---------------------------------------------------------------------------


class TestRouteAfterClassify:
    """Tests for route_after_classify routing function."""

    def test_build_intent_routes_to_clarify(self):
        """build_intent should route to clarify node."""
        state: ForgeState = {"intent": "build_intent"}
        assert route_after_classify(state) == "clarify"

    def test_status_query_routes_to_status(self):
        """status_query should route to status node."""
        state: ForgeState = {"intent": "status_query"}
        assert route_after_classify(state) == "status"

    def test_interrupt_routes_to_interrupt(self):
        """interrupt should route to interrupt node."""
        state: ForgeState = {"intent": "interrupt"}
        assert route_after_classify(state) == "interrupt"

    def test_natural_language_routes_to_end(self):
        """natural_language (unknown) should route to END."""
        state: ForgeState = {"intent": "natural_language"}
        assert route_after_classify(state) == _END

    def test_empty_intent_routes_to_end(self):
        """Empty intent should route to END."""
        state: ForgeState = {"intent": ""}
        assert route_after_classify(state) == _END

    def test_missing_intent_routes_to_end(self):
        """Missing intent field should route to END."""
        state: ForgeState = {}
        assert route_after_classify(state) == _END


class TestRouteAfterVerify:
    """Tests for route_after_verify routing function."""

    def test_passed_verification_routes_to_commit(self):
        """When verification passes, route to commit."""
        state: ForgeState = {
            "current_task_id": "t1",
            "verification_results": {"t1": {"passed": True}},
        }
        assert route_after_verify(state) == "commit"

    def test_failed_verification_routes_to_policy(self):
        """When verification fails, route to policy."""
        state: ForgeState = {
            "current_task_id": "t1",
            "verification_results": {"t1": {"passed": False}},
        }
        assert route_after_verify(state) == "policy"

    def test_missing_results_routes_to_policy(self):
        """When no verification results exist, route to policy."""
        state: ForgeState = {
            "current_task_id": "t1",
            "verification_results": {},
        }
        assert route_after_verify(state) == "policy"

    def test_no_task_id_routes_to_policy(self):
        """When no current_task_id, route to policy."""
        state: ForgeState = {"verification_results": {}}
        assert route_after_verify(state) == "policy"


class TestRouteAfterPolicy:
    """Tests for route_after_policy routing function."""

    def test_always_routes_to_execute(self):
        """Policy always routes back to execute."""
        state: ForgeState = {"decisions": ["retry_task_t1"]}
        assert route_after_policy(state) == "execute"

    def test_empty_state_routes_to_execute(self):
        """Even with empty state, routes to execute."""
        state: ForgeState = {}
        assert route_after_policy(state) == "execute"


class TestRouteAfterCommit:
    """Tests for route_after_commit routing function."""

    def test_all_tasks_done_routes_to_doc_update(self):
        """When all tasks are done, route to doc_update."""
        state: ForgeState = {"all_tasks_done": True}
        assert route_after_commit(state) == "doc_update"

    def test_tasks_remaining_routes_to_execute(self):
        """When tasks remain, route back to execute."""
        state: ForgeState = {"all_tasks_done": False}
        assert route_after_commit(state) == "execute"

    def test_missing_flag_routes_to_execute(self):
        """When all_tasks_done is missing, route to execute."""
        state: ForgeState = {}
        assert route_after_commit(state) == "execute"


# ---------------------------------------------------------------------------
# Tests: assemble_deps
# ---------------------------------------------------------------------------


class TestAssembleDeps:
    """Tests for assemble_deps function."""

    def test_returns_runtime_deps_with_all_fields(self):
        """assemble_deps returns a RuntimeDeps with all fields populated."""
        from app.workflow.bootstrap import assemble_deps
        from app.workflow.deps import RuntimeDeps

        deps = assemble_deps(config_dir="nonexistent_config")

        assert isinstance(deps, RuntimeDeps)
        assert deps.event_bus is not None
        assert deps.registry is not None
        assert deps.secret_holder is not None
        assert deps.session_manager is not None
        assert deps.audit_trail is not None
        assert deps.recovery is not None
        assert deps.model_router is not None
        assert deps.workspace_manager is not None
        assert deps.inspector is not None
        assert deps.interrupt_handler is not None
        assert deps.mode_evaluator is not None
        assert deps.policy_engine is not None
        assert deps.learning_recorder is not None
        assert deps.health_monitor is not None
        assert deps.config_dir == "nonexistent_config"

    def test_event_bus_has_audit_trail_subscriber(self):
        """assemble_deps wires audit_trail as an event bus subscriber."""
        from app.workflow.bootstrap import assemble_deps

        deps = assemble_deps()

        # Audit trail should be subscribed
        subscriber_ids = [s.subscriber_id for s in deps.event_bus._subscribers]
        assert "audit_trail" in subscriber_ids


# ---------------------------------------------------------------------------
# Tests: bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    """Tests for the bootstrap function."""

    async def test_bootstrap_completes_without_config(self):
        """Bootstrap succeeds when config directory doesn't exist."""
        from app.workflow.bootstrap import assemble_deps, bootstrap

        deps = assemble_deps(config_dir="nonexistent_test_config_dir")

        # Should not raise — discovery is skipped when config dir is missing
        await bootstrap(deps)

        # Health monitor should be running
        assert deps.health_monitor.running is True

        # Clean up
        await deps.health_monitor.stop()

    async def test_bootstrap_emits_forge_ready(self):
        """Bootstrap emits forge.ready event (via ModeEvaluator.evaluate_and_emit)."""
        from app.workflow.bootstrap import assemble_deps, bootstrap

        deps = assemble_deps(config_dir="nonexistent_test_config_dir")

        # Track published events via subscription (not monkeypatching)
        published_events = []

        async def capture_event(event):
            published_events.append(event)

        deps.event_bus.subscribe("*", capture_event, subscriber_id="test_capture")

        await bootstrap(deps)

        # Check that forge.ready was emitted (by ModeEvaluator)
        ready_events = [
            e for e in published_events
            if hasattr(e, "type") and e.type.value == "forge.ready"
        ]
        assert len(ready_events) >= 1

        # Clean up
        await deps.health_monitor.stop()


# ---------------------------------------------------------------------------
# Tests: create_app
# ---------------------------------------------------------------------------


class TestCreateApp:
    """Tests for the create_app function."""

    def test_returns_fastapi_instance(self):
        """create_app returns a FastAPI application."""
        from fastapi import FastAPI

        from app.workflow.app import create_app

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_app_has_workflow_invoke_route(self):
        """App has a POST /workflow/invoke route."""
        from app.workflow.app import create_app

        app = create_app()
        routes = []
        for route in app.routes:
            if hasattr(route, 'path'):
                routes.append(route.path)
            elif hasattr(route, 'routes'):
                for sub in route.routes:
                    if hasattr(sub, 'path'):
                        routes.append(sub.path)
        assert "/workflow/invoke" in routes

    def test_app_has_health_route(self):
        """App has a GET /health route."""
        from app.workflow.app import create_app

        app = create_app()
        routes = []
        for route in app.routes:
            if hasattr(route, 'path'):
                routes.append(route.path)
            elif hasattr(route, 'routes'):
                for sub in route.routes:
                    if hasattr(sub, 'path'):
                        routes.append(sub.path)
        assert "/health" in routes

    def test_no_included_router_wrappers_in_routes(self):
        """All routes in app.routes have a .path attribute.

        Regression: previously the manual `for route in api_app.routes:
        app.routes.append(route)` pattern left _IncludedRouter wrappers that
        lack .path, breaking url_path_for() and route introspection.
        """
        from app.workflow.app import create_app

        app = create_app()
        for route in app.routes:
            assert hasattr(route, 'path'), (
                f"{type(route).__name__} has no .path — route list not flattened. "
                "Use include_router or flatten _IncludedRouter after appending."
            )

    def test_no_duplicate_route_paths(self):
        """No (path, method) combination appears more than once in app.routes.

        Regression: route copying patterns and router definitions with duplicate
        decorators can produce duplicate routes, causing ambiguous path matches.
        Note: POST /sessions and GET /sessions are NOT duplicates — same path,
        different HTTP methods are distinct routes.
        """
        from collections import Counter

        from fastapi.routing import APIRoute

        from app.workflow.app import create_app

        app = create_app()
        path_methods = []
        for route in app.routes:
            if isinstance(route, APIRoute):
                # route.methods is a frozenset; convert to tuple for hashing
                path_methods.append((route.path, tuple(sorted(route.methods))))
        dupes = [(p, tuple(sorted(m))) for p, m in Counter(path_methods).items() if m > 1]
        assert not dupes, f"Duplicate (path, method) routes found: {dupes}"
