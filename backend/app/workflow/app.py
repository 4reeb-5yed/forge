"""FastAPI application — Forge Runtime entry point with lifespan management.

Creates the FastAPI app with:
- Lifespan: runs bootstrap on startup, stops health monitor on shutdown
- Stores the compiled graph in app.state for invocation
- Exposes POST /workflow/invoke endpoint

Requirements: 16.1, 16.2, 16.3, 16.4
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.runtime.models import ForgeState
from app.workflow.bootstrap import assemble_deps, bootstrap

logger = logging.getLogger(__name__)


class InvokeRequest(BaseModel):
    """Request body for the /workflow/invoke endpoint."""

    message: str
    session_id: str = "default"
    build_mode: str = "new"


class InvokeResponse(BaseModel):
    """Response body from the /workflow/invoke endpoint."""

    status: str
    commit_shas: list[str] = []
    errors: list[dict[str, Any]] = []
    node_path: list[str] = []


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Application lifespan: bootstrap on startup, cleanup on shutdown."""
    # Startup
    deps = assemble_deps()
    app.state.deps = deps

    await bootstrap(deps)

    # ──────────────────────────────────────────────────────────────────
    # Wire RuntimeDeps into the API layer's AppDependencies.
    # This ensures all API endpoints use the same RuntimeInspector,
    # SessionManager, InterruptHandler, and EventBus instances that the
    # workflow layer uses — single shared set of runtime services.
    # ──────────────────────────────────────────────────────────────────
    from app.api import AppDependencies, SessionEventStore, set_deps as set_api_deps

    event_store = getattr(app.state, "event_store", None) or SessionEventStore()

    # Subscribe the event store to the event bus so WebSocket gets events
    async def _forward_event(event):
        await event_store.append(event.session_id, event)

    deps.event_bus.subscribe("*", _forward_event, subscriber_id="api_event_store")

    api_deps = AppDependencies(
        session_manager=deps.session_manager,
        inspector=deps.inspector,
        interrupt_handler=deps.interrupt_handler,
        event_store=event_store,
    )
    set_api_deps(api_deps)

    logger.info(
        "API layer wired to RuntimeDeps (inspector=%s, session_manager=%s, interrupt_handler=%s)",
        type(deps.inspector).__name__,
        type(deps.session_manager).__name__,
        type(deps.interrupt_handler).__name__,
    )

    # Build and store the compiled graph
    try:
        from app.workflow.graph import build_forge_graph

        graph = build_forge_graph(deps)
        app.state.graph = graph
        logger.info("Workflow graph compiled and stored in app.state")
    except ImportError:
        logger.warning(
            "LangGraph not available — graph not compiled. "
            "The /workflow/invoke endpoint will return 503."
        )
        app.state.graph = None

    yield

    # Shutdown
    try:
        await deps.health_monitor.stop()
    except Exception:
        logger.exception("Error stopping health monitor during shutdown")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application with the wired workflow.

    Returns:
        A configured FastAPI app with lifespan management, the compiled
        LangGraph workflow in app.state, and all endpoints (sessions,
        control, inspection, WebSocket, workflow invocation).
    """
    app = FastAPI(
        title="Forge Runtime",
        description="Autonomous Software Engineering Runtime",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # ──────────────────────────────────────────────────────────────────
    # Mount the sessions/control/inspection/WebSocket endpoints from the
    # API layer. These are defined in app.api and include:
    #   POST/GET /sessions, GET/DELETE /sessions/{id},
    #   POST /sessions/{id}/interrupt|resume|redirect|stop,
    #   GET /sessions/{id}/status|explain,
    #   GET /capabilities,
    #   WS /sessions/{id}/events
    #
    # DEPENDENCY WIRING: The API layer's `get_deps()` function is wired
    # to return an AppDependencies backed by the RuntimeDeps assembled
    # during the lifespan (bootstrap). This is done via a startup event
    # that calls set_deps() after the lifespan initializes RuntimeDeps.
    # ──────────────────────────────────────────────────────────────────
    from app.api import create_app as create_api_app, AppDependencies, set_deps, SessionEventStore

    # Create the event store (shared between API and event bus)
    event_store = SessionEventStore()

    api_app = create_api_app()
    # Include all routes from the API app into this app
    for route in api_app.routes:
        app.routes.append(route)

    # Store the event store so the lifespan can wire it
    app.state.event_store = event_store

    @app.post("/workflow/invoke", response_model=InvokeResponse)
    async def invoke_workflow(request: InvokeRequest) -> InvokeResponse:
        """Invoke the Forge workflow with a user message.

        Constructs an initial ForgeState, runs it through the compiled
        LangGraph state machine, and returns the final state summary.
        """
        graph = app.state.graph
        if graph is None:
            raise HTTPException(
                status_code=503,
                detail="Workflow graph not available. LangGraph may not be installed.",
            )

        initial_state: ForgeState = {
            "session_id": request.session_id,
            "message": request.message,
            "build_mode": request.build_mode,  # type: ignore[typeddict-item]
            "status": "received",
            "tasks": [],
            "errors": [],
            "decisions": [],
            "node_path": [],
            "commit_shas": [],
            "doc_updates": [],
            "verification_results": {},
            "needs_clarification": False,
            "all_tasks_done": False,
            "approval_pending": False,
        }

        try:
            final_state = await graph.ainvoke(initial_state)
        except Exception as exc:
            logger.exception("Workflow invocation failed")
            raise HTTPException(
                status_code=500,
                detail=f"Workflow execution error: {exc}",
            ) from exc

        return InvokeResponse(
            status=final_state.get("status", "unknown"),
            commit_shas=final_state.get("commit_shas", []),
            errors=final_state.get("errors", []),
            node_path=final_state.get("node_path", []),
        )

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """Basic health check endpoint."""
        return {"status": "ok"}

    return app
