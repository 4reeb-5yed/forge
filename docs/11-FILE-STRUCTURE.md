# Complete File Structure

Every file in the Forge repository (excluding `.venv/`, `__pycache__/`, `.pytest_cache/`, `.hypothesis/`, `node_modules/`):

```
forge/
├── .env.docker                                    # Docker environment template
├── .gitignore                                     # Git ignore rules
├── README.md                                      # Project README
├── docker-compose.yml                             # PostgreSQL + Forge API services
│
├── docs/                                          # Documentation suite
│   ├── README.md                                  # Documentation index
│   ├── 01-OVERVIEW.md                             # Project overview + status
│   ├── 02-ARCHITECTURE.md                         # 6-layer architecture
│   ├── 03-RUNTIME-MODULES.md                      # All 27 runtime modules
│   ├── 04-WORKFLOW.md                             # LangGraph state machine
│   ├── 05-ADAPTERS.md                             # OpenRouter, GitHub, Aider
│   ├── 06-DATABASE.md                             # PostgreSQL schema + stores
│   ├── 07-FRONTEND.md                             # Next.js components
│   ├── 08-DEPLOYMENT.md                           # Docker, env vars, scaling
│   ├── 09-TESTING.md                              # Test strategy + properties
│   ├── 10-FUTURE.md                               # Roadmap + tradeoffs
│   ├── 11-FILE-STRUCTURE.md                       # This file
│   └── 12-SECURITY.md                             # Sandbox, scope check, secret isolation
│
├── frontend/                                      # Next.js responsive UI
│   ├── app/
│   │   ├── globals.css                            # Tailwind imports + custom styles
│   │   ├── layout.tsx                             # Root layout (dark theme, Inter font)
│   │   └── page.tsx                               # Main page (chat + sidebar + events)
│   ├── components/
│   │   ├── ChatInput.tsx                          # Message input with send button
│   │   ├── ChatMessage.tsx                        # Individual message rendering
│   │   ├── EventLog.tsx                           # Real-time WebSocket event stream
│   │   ├── SessionList.tsx                        # Sidebar session management
│   │   └── StatusBar.tsx                          # Runtime status + controls
│   ├── lib/
│   │   └── api.ts                                 # API client (REST + WebSocket)
│   ├── next.config.js                              # API rewrites to backend
│   ├── package.json                               # Dependencies
│   ├── postcss.config.mjs                         # PostCSS for Tailwind
│   ├── tailwind.config.ts                         # Dark theme colors
│   └── tsconfig.json                              # TypeScript config
│
├── backend/                                       # Python backend
│   ├── Dockerfile                                 # Multi-stage build
│   ├── Dockerfile.sandbox                         # Aider sandbox image (read-only, non-root)
│   ├── main.py                                    # uvicorn entry point
│   ├── pyproject.toml                             # Dependencies + config
│   ├── .env.example                               # Local env template
│   ├── alembic.ini                                # Alembic migration config
│   │
│   ├── alembic/                                   # Database migrations
│   │   ├── __init__.py
│   │   ├── env.py                                 # Async migration environment
│   │   ├── script.py.mako                         # Migration template
│   │   └── versions/
│   │       ├── __init__.py
│   │       └── 001_initial_schema.py              # sessions, audit_log, checkpoints, learning
│   │
│   ├── config/                                    # YAML configuration
│   │   ├── adapters.yaml                          # Adapter discovery (OpenRouter, GitHub, Aider)
│   │   ├── models.yaml                            # AI provider fallback chains
│   │   ├── policies.yaml                          # Retry/escalation rules
│   │   ├── rate_limits.yaml                       # Token/cost limits
│   │   ├── tools.yaml                             # Coding tool config
│   │   └── verification.yaml                      # Verifier stage definitions
│   │
│   ├── app/
│   │   ├── __init__.py
│   │   │
│   │   ├── api/                                   # Layer 2: Application
│   │   │   ├── __init__.py                        # FastAPI endpoints (REST + WS)
│   │   │   └── auth.py                            # Bearer token authentication
│   │   │
│   │   ├── adapters/                              # Layer 5: Adapters
│   │   │   ├── __init__.py                        # Exports all adapters
│   │   │   ├── openrouter.py                      # OpenRouter AI provider
│   │   │   ├── github_vcs.py                      # GitHub VCS (git subprocess)
│   │   │   └── aider_tool.py                      # Aider coding tool (subprocess)
│   │   │
│   │   ├── workflow/                              # Layer 3: LangGraph Workflow
│   │   │   ├── __init__.py
│   │   │   ├── app.py                             # FastAPI app factory + lifespan
│   │   │   ├── bootstrap.py                       # Startup sequence + assemble_deps()
│   │   │   ├── deps.py                            # RuntimeDeps container
│   │   │   ├── graph.py                           # Graph builder (13 nodes + edges)
│   │   │   ├── routing.py                         # Conditional edge routing functions
│   │   │   └── nodes/                             # 13 node functions
│   │   │       ├── __init__.py                    # Exports all node factories
│   │   │       ├── intake.py                      # Validate + initialize
│   │   │       ├── classify.py                    # Deterministic intent classification
│   │   │       ├── clarify.py                     # Gather missing inputs
│   │   │       ├── architect.py                   # Invoke Architect role → spec + tasks
│   │   │       ├── plan.py                        # Topological sort of tasks
│   │   │       ├── execute.py                     # Dispatch task in workspace
│   │   │       ├── verify.py                      # Run verification pipeline
│   │   │       ├── policy.py                      # Decide retry/skip/escalate
│   │   │       ├── commit.py                      # Commit + advance index
│   │   │       ├── doc_update.py                  # Update documentation
│   │   │       ├── finalize.py                    # Push + emit build.done
│   │   │       ├── status.py                      # Answer status queries
│   │   │       └── interrupt.py                   # Handle pause/stop
│   │   │
│   │   ├── runtime/                               # Layer 4: Runtime (27 modules)
│   │   │   ├── __init__.py
│   │   │   ├── models.py                          # ForgeState, Task, Capability, Role, etc.
│   │   │   ├── protocols.py                       # Plugin protocol interfaces
│   │   │   ├── types.py                           # Health, ToolResult
│   │   │   │
│   │   │   ├── events/                            # Event Bus
│   │   │   │   ├── __init__.py
│   │   │   │   ├── bus.py                         # EventBus (pub/sub, ordering, replay)
│   │   │   │   ├── backpressure.py                # Bounded subscriber queues
│   │   │   │   └── models.py                      # Event, EventType, DecisionRecord
│   │   │   │
│   │   │   ├── registry/                          # Capability Registry
│   │   │   │   └── __init__.py                    # Register/deregister/resolve
│   │   │   │
│   │   │   ├── discovery/                         # Bootstrap Discovery
│   │   │   │   └── __init__.py                    # Probe resources, register healthy
│   │   │   │
│   │   │   ├── health/                            # Health Monitor
│   │   │   │   └── __init__.py                    # Continuous re-probing
│   │   │   │
│   │   │   ├── mode/                              # Mode Evaluator
│   │   │   │   └── __init__.py                    # OPERATIONAL vs DEGRADED
│   │   │   │
│   │   │   ├── router/                            # Model Router
│   │   │   │   └── __init__.py                    # Fallback chains + circuit breaker
│   │   │   │
│   │   │   ├── budget/                            # Session Budget
│   │   │   │   └── __init__.py                    # Token/cost governance
│   │   │   │
│   │   │   ├── secrets/                           # Secret Holder
│   │   │   │   └── __init__.py                    # In-memory + redaction
│   │   │   │
│   │   │   ├── session/                           # Session Manager
│   │   │   │   └── __init__.py                    # CRUD + secret storage
│   │   │   │
│   │   │   ├── audit/                             # Audit Trail
│   │   │   │   └── __init__.py                    # Event projection + decisions
│   │   │   │
│   │   │   ├── classifier/                        # Intent Classifier
│   │   │   │   ├── __init__.py                    # Rules-based classifier
│   │   │   │   └── router.py                      # Intent → action routing
│   │   │   │
│   │   │   ├── clarification/                     # Clarification Engine
│   │   │   │   └── __init__.py                    # SessionContext + questions
│   │   │   │
│   │   │   ├── specification/                     # Specification Generator
│   │   │   │   └── __init__.py                    # Architect role + artifact
│   │   │   │
│   │   │   ├── planner/                           # Task Planner
│   │   │   │   └── __init__.py                    # DAG + topological sort
│   │   │   │
│   │   │   ├── workspace/                         # Workspace Manager
│   │   │   │   └── __init__.py                    # Create/destroy sandboxes
│   │   │   │
│   │   │   ├── dispatcher/                        # Task Dispatcher
│   │   │   │   └── __init__.py                    # Sequential execution + isolation
│   │   │   │
│   │   │   ├── verification/                      # Verification Pipeline
│   │   │   │   ├── __init__.py                    # Advisory + blocking stages
│   │   │   │   └── scope_check.py                 # Pre-commit diff-scope security check
│   │   │   │
│   │   │   ├── policies/                          # Policy Engine
│   │   │   │   └── __init__.py                    # Retry/escalate/skip decisions
│   │   │   │
│   │   │   ├── commit/                            # Commit Workflow
│   │   │   │   └── __init__.py                    # Stage + commit + push
│   │   │   │
│   │   │   ├── finalization/                      # Finalization
│   │   │   │   └── __init__.py                    # Push + build summary
│   │   │   │
│   │   │   ├── documentation/                     # Documentation Manager
│   │   │   │   └── __init__.py                    # Twin diff + doc updates
│   │   │   │
│   │   │   ├── inspector/                         # Runtime Inspector
│   │   │   │   └── __init__.py                    # Query-only facade (no AI)
│   │   │   │
│   │   │   ├── recovery/                          # Crash Recovery
│   │   │   │   └── __init__.py                    # Checkpoints + resume
│   │   │   │
│   │   │   ├── interrupt/                         # Interrupt Handler
│   │   │   │   └── __init__.py                    # Pause/resume/stop
│   │   │   │
│   │   │   ├── learning/                          # Learning Recorder
│   │   │   │   └── __init__.py                    # Outcome recording
│   │   │   │
│   │   │   └── boundaries/                        # Boundary Checker
│   │   │       └── __init__.py                    # Layer enforcement
│   │   │
│   │   ├── db/                                    # Layer 6: Persistence
│   │   │   ├── __init__.py                        # Exports pool + stores
│   │   │   ├── pool.py                            # asyncpg connection pool
│   │   │   ├── session_store.py                   # Session CRUD
│   │   │   ├── audit_store.py                     # Event recording
│   │   │   ├── checkpoint_store.py                # Checkpoint write/read
│   │   │   └── learning_store.py                  # Outcome recording
│   │   │
│   │   ├── config/                                # Config stubs
│   │   │   └── __init__.py
│   │   │
│   │   ├── shared/                               # Canonical source for shared types
│   │   │   └── __init__.py                       # Health, ToolResult, PermanentError (single source of truth)
│   │   │
│   │   └── boundaries.py                          # Layer boundary enforcement
│   │
│   ├── docs/                                      # Legacy backend docs
│   │   ├── ARCHITECTURE.md
│   │   └── DEVELOPMENT.md
│   │
│   └── tests/                                     # 1,343+ tests
│       ├── __init__.py
│       ├── test_adapters.py                       # Adapter unit tests (25)
│       ├── test_api.py                            # API endpoint tests (34)
│       ├── test_audit.py                          # AuditTrail tests
│       ├── test_audit_properties.py               # Audit property tests
│       ├── test_auth.py                           # Authentication tests (25)
│       ├── test_backpressure.py                   # Backpressure queue tests
│       ├── test_boundaries.py                     # Layer boundary tests
│       ├── test_budget.py                         # Budget tests
│       ├── test_budget_properties.py              # Budget property tests
│       ├── test_circuit_breaker.py                # Circuit breaker tests
│       ├── test_clarification.py                  # Clarification tests
│       ├── test_classifier.py                     # Classifier tests
│       ├── test_commit_finalization_docs.py       # Commit/finalize/doc tests
│       ├── test_commit_workflow.py                # Commit workflow tests
│       ├── test_discovery.py                      # Discovery tests
│       ├── test_discovery_properties.py           # Discovery property tests
│       ├── test_dispatcher.py                     # Dispatcher tests
│       ├── test_documentation.py                  # Documentation tests
│       ├── test_event_bus.py                      # EventBus tests
│       ├── test_event_properties.py               # Event property tests
│       ├── test_finalization.py                   # Finalization tests
│       ├── test_health_monitor.py                 # Health monitor tests
│       ├── test_inspector.py                      # Inspector tests
│       ├── test_inspector_properties.py           # Inspector property tests
│       ├── test_intent_router.py                  # Intent router tests
│       ├── test_interrupt.py                      # Interrupt tests
│       ├── test_learning.py                       # Learning recorder tests
│       ├── test_mode_evaluation.py                # Mode evaluation tests
│       ├── test_planner.py                        # Planner tests
│       ├── test_planner_properties.py             # Planner property tests
│       ├── test_policy_engine.py                  # Policy engine tests
│       ├── test_property_doc_drift.py             # Doc drift property tests
│       ├── test_protocols.py                      # Protocol tests
│       ├── test_recovery.py                       # Recovery tests
│       ├── test_registry.py                       # Registry tests
│       ├── test_router.py                         # Router tests
│       ├── test_router_events.py                  # Router event tests
│       ├── test_router_properties.py              # Router property tests
│       ├── test_secret_properties.py              # Secret property tests
│       ├── test_secrets.py                        # Secret holder tests
│       ├── test_session.py                        # Session tests
│       ├── test_specification.py                  # Specification tests
│       ├── test_verification.py                   # Verification tests
│       ├── test_verification_properties.py        # Verification property tests
│       ├── test_workflow_infra.py                  # Workflow infra tests
│       ├── test_workflow_nodes.py                  # Workflow node tests
│       ├── test_workspace.py                      # Workspace tests
│       ├── test_workspace_properties.py           # Workspace property tests
│       ├── test_dependency_wiring.py              # API↔RuntimeDeps wiring regression tests
│       ├── test_sandboxed_aider.py                # SandboxedAiderTool tests (44)
│       └── test_scope_check.py                    # Scope check + workspace limit tests
│
└── .kiro/specs/                                   # Spec-driven development artifacts
    ├── forge-runtime/                             # Runtime spec (76 tasks, all complete)
    │   ├── .config.kiro
    │   ├── design.md
    │   ├── requirements.md
    │   └── tasks.md
    ├── forge-workflow/                             # Workflow spec (20 tasks, all complete)
    │   ├── .config.kiro
    │   ├── design.md
    │   ├── requirements.md
    │   └── tasks.md
    └── forge-infra/                               # Infrastructure spec (19 tasks, complete)
        ├── .config.kiro
        └── tasks.md
```

## File Counts

| Directory | Files | Description |
|-----------|-------|-------------|
| `frontend/` | 14 | Next.js app, components, config |
| `backend/app/api/` | 2 | REST endpoints + auth |
| `backend/app/adapters/` | 5 | OpenRouter, GitHub, Aider, Sandboxed Aider |
| `backend/app/workflow/` | 7 + 13 nodes = 20 | State machine + node functions |
| `backend/app/runtime/` | 27 | Core business logic (27 modules) |
| `backend/app/db/` | 6 | PostgreSQL stores |
| `backend/alembic/` | 4 | Database migrations |
| `backend/config/` | 6 | YAML configuration |
| `backend/tests/` | 48 | Unit + property-based + integration tests |
| `docs/` | 13 | Documentation suite |
| Root | 4 | README, docker-compose, .gitignore, .env.docker |
| **Total (project files)** | **~165** | Excluding .venv, .kiro specs |
