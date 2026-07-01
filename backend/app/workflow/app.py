"""FastAPI application — Forge Runtime entry point with lifespan management.

Creates the FastAPI app with:
- Lifespan: runs bootstrap on startup, stops health monitor on shutdown
- Stores the compiled graph in app.state for invocation
- Exposes POST /workflow/invoke endpoint

Requirements: 16.1, 16.2, 16.3, 16.4
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.api.auth import require_auth
from app.api.errors import register_error_handlers
from app.runtime.models import ForgeState
from app.workflow.bootstrap import assemble_deps, bootstrap

logger = logging.getLogger(__name__)


class InvokeRequest(BaseModel):
    """Request body for the /workflow/invoke endpoint."""

    message: str
    session_id: str = "default"
    build_mode: str = "new"
    repo_url: str = ""  # Target repository URL for clone/push


class InvokeResponse(BaseModel):
    """Response body from the /workflow/invoke endpoint."""

    status: str
    commit_shas: list[str] = []
    errors: list[dict[str, Any]] = []
    node_path: list[str] = []


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Application lifespan: bootstrap on startup, cleanup on shutdown."""
    # Startup: Initialize PostgreSQL pool first if DATABASE_URL is available
    pool = None
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url:
        try:
            from app.db.pool import create_pool
            pool = await create_pool()
            logger.info("PostgreSQL pool created during startup")
        except Exception as exc:
            logger.warning("Failed to create PostgreSQL pool: %s — using in-memory stores", exc)

    # Wire pool into persistence module for later use
    if pool:
        from app.runtime import persistence as persist_module
        persist_module._pool = pool

    # Initialize and wire approval manager
    from app.runtime.approval import ApprovalManager, set_approval_manager
    approval_manager = ApprovalManager()
    set_approval_manager(approval_manager)
    logger.info("Approval manager initialized")

    # Initialize and wire scheduler (lazy-loaded via singleton)
    from app.runtime.scheduler import get_scheduler
    _ = get_scheduler()  # Trigger lazy initialization
    logger.info("Session scheduler initialized")

    # Initialize and wire timeout manager (lazy-loaded via singleton)
    from app.runtime.build_timeout import get_timeout_manager
    _ = get_timeout_manager()  # Trigger lazy initialization
    logger.info("Build timeout manager initialized")

    # Initialize and wire learning engine (lazy-loaded via singleton)
    from app.runtime.learning_engine import get_learning_engine
    _ = get_learning_engine()  # Trigger lazy initialization
    logger.info("Learning engine initialized")

    # Initialize and wire stream router (lazy-loaded via singleton)
    from app.runtime.stream_router import set_stream_router, StreamRouter
    # StreamRouter will be wired after deps is assembled (needs model_router)

    deps = assemble_deps()
    app.state.deps = deps

    # Wire event emitters to all managers after deps is available
    approval_manager.update_event_emitter(deps.event_bus.publish)
    logger.info("Approval manager event emitter wired")

    scheduler = get_scheduler()
    scheduler.update_event_emitter(deps.event_bus.publish)
    logger.info("Session scheduler event emitter wired")

    timeout_manager = get_timeout_manager()
    timeout_manager.update_event_emitter(deps.event_bus.publish)
    timeout_manager.update_interrupt_handler(deps.interrupt_handler)
    logger.info("Build timeout manager wired with event emitter and interrupt handler")

    # Wire StreamRouter after deps is assembled (needs model_router and event_bus)
    try:
        from app.runtime.stream_router import set_stream_router, StreamRouter
        stream_router = StreamRouter(
            model_router=deps.model_router,
            event_emitter=deps.event_bus.publish,
        )
        set_stream_router(stream_router)
        logger.info("Stream router initialized")
    except Exception as exc:
        logger.warning("Failed to initialize stream router: %s", exc)

    await bootstrap(deps)

    # ──────────────────────────────────────────────────────────────────
    # Wire RuntimeDeps into the API layer's AppDependencies.
    # CRITICAL FIX: Set _deps directly on the api module object to avoid
    # any import aliasing issues. Route handlers call get_deps() which
    # reads this module-level variable at request time.
    # ──────────────────────────────────────────────────────────────────
    import app.api as api_module

    event_store = getattr(app.state, "event_store", None) or api_module.SessionEventStore()

    # Subscribe the event store to the event bus so WebSocket gets events
    async def _forward_event(event):
        await event_store.append(event.session_id, event)

    deps.event_bus.subscribe("*", _forward_event, subscriber_id="api_event_store")

    api_deps = api_module.AppDependencies(
        session_manager=deps.session_manager,
        inspector=deps.inspector,
        interrupt_handler=deps.interrupt_handler,
        event_store=event_store,
        config_service=deps.config_service,
        approval_manager=approval_manager,
    )
    # Set directly on the module global (most reliable method)
    api_module._deps = api_deps
    # Also store config_service directly on the module for easy access by config routes
    api_module._config_service = deps.config_service

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

    # Close PostgreSQL pool if it was created
    if pool:
        try:
            from app.db.pool import close_pool
            await close_pool()
            logger.info("PostgreSQL pool closed during shutdown")
        except Exception:
            logger.exception("Error closing PostgreSQL pool during shutdown")


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
        dependencies=[Depends(require_auth)],
    )

    # Register structured error handlers (ErrorEnvelope format)
    register_error_handlers(app)

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
    from app.api import create_app as create_api_app, SessionEventStore

    # Create the event store (shared between API and event bus)
    event_store = SessionEventStore()

    api_app = create_api_app()
    # Include all routes from the API app into this app
    for route in api_app.routes:
        app.routes.append(route)

    # Store the event store so the lifespan can wire it
    app.state.event_store = event_store

    # Mount the config API router
    from app.api.config import config_router

    app.include_router(config_router)

    # Mount the approval API router
    from app.api.approval import approval_router

    app.include_router(approval_router)

    # Mount the recovery API router
    from app.api.recovery import recovery_router

    app.include_router(recovery_router)

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
            "repo_url": request.repo_url,
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

    @app.get("/health", dependencies=[])
    async def health_check() -> dict[str, Any]:
        """Enhanced health check endpoint — no authentication required.

        Returns per-component health status, overall aggregated status,
        and configured boolean. Suitable for container orchestrator probes.
        """
        import app.api as api_module

        # Critical components: openrouter, database
        # Non-critical components: github, docker, event_bus
        critical_components = {"openrouter", "database"}

        # Default component health when ConfigService is not available
        default_components = {
            "openrouter": {"status": "healthy", "message": ""},
            "github": {"status": "healthy", "message": ""},
            "docker": {"status": "healthy", "message": ""},
            "database": {"status": "healthy", "message": ""},
            "event_bus": {"status": "healthy", "message": ""},
        }

        configured = False
        components = default_components

        # Try to get real health from ConfigService
        config_service = getattr(api_module, "_config_service", None)
        if config_service is None:
            # Also try from deps
            deps_obj = getattr(api_module, "_deps", None)
            if deps_obj is not None:
                config_service = getattr(deps_obj, "config_service", None)

        if config_service is not None:
            try:
                config_data = await config_service.get_config()
                configured = config_data.get("configured", False)
            except Exception:
                configured = False

            try:
                health_data = await config_service.get_component_health()
                components = {}
                for name, comp_health in health_data.items():
                    if hasattr(comp_health, "status"):
                        components[name] = {
                            "status": comp_health.status,
                            "message": getattr(comp_health, "message", ""),
                        }
                    else:
                        components[name] = comp_health
            except Exception:
                pass

        # Aggregate status
        overall = "healthy"
        for name, comp in components.items():
            comp_status = comp.get("status", "healthy") if isinstance(comp, dict) else getattr(comp, "status", "healthy")
            if comp_status == "unhealthy":
                if name in critical_components:
                    overall = "unhealthy"
                    break
                else:
                    overall = "degraded"
            elif comp_status == "degraded" and overall == "healthy":
                overall = "degraded"

        return {
            "status": overall,
            "configured": configured,
            "components": components,
        }

    return app
