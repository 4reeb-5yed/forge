# Forge

**Autonomous Software Engineering Runtime**

Forge is an autonomous software engineering runtime. You supply a GitHub repository URL and a plain-English goal — Forge plans, builds, reviews, verifies, and commits code into that repository, streaming every decision and artifact back through a real-time interface.

## Architecture

Forge follows a strict 5-layer architecture with adjacent-only communication:

```
┌─────────────────────────────────────────────┐
│  Presentation (Next.js — future)            │
├─────────────────────────────────────────────┤
│  Application (FastAPI REST + WebSocket)      │
├─────────────────────────────────────────────┤
│  Runtime (asyncio core — all logic here)     │
├─────────────────────────────────────────────┤
│  Adapter (plugin protocols)                  │
├─────────────────────────────────────────────┤
│  Infrastructure (PostgreSQL, R2, GitHub)     │
└─────────────────────────────────────────────┘
```

### Guiding Principles

| Principle | Meaning |
|-----------|---------|
| **Deterministic** | Same inputs + config + recorded outputs reproduce the same path |
| **Observable** | Every state transition emits a typed event |
| **Modular** | One file, one protocol, one name, zero edits to existing code |
| **Replaceable** | Any adapter swaps without touching the runtime core |
| **Explainable** | Every decision answerable from state + audit trail, never by asking an LLM |
| **Resilient** | Failures isolated, retried under policy, survive process restart |

## Current Status

The runtime core and LangGraph workflow are fully implemented with **1,240+ passing tests** covering all 26 requirements and 12 correctness properties. The system is runnable end-to-end with in-memory stores and mock adapters.

**Runnable now:**
```bash
cd backend
pip install -e ".[dev]"
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then invoke a build:
```bash
curl -X POST http://localhost:8000/workflow/invoke \
  -H "Content-Type: application/json" \
  -d '{"message": "Build user authentication with JWT", "session_id": "demo-1"}'
```

## Project Structure

```
backend/
├── app/
│   ├── api/                    # Application Layer (FastAPI endpoints + auth)
│   ├── adapters/               # Adapter Layer (plugin protocol stubs)
│   ├── workflow/               # LangGraph Workflow (state machine wiring)
│   │   ├── nodes/             # 13 node functions (intake → finalize)
│   │   ├── routing.py         # Conditional edge routing functions
│   │   ├── graph.py           # Graph builder (StateGraph construction)
│   │   ├── bootstrap.py       # Startup sequence + assemble_deps()
│   │   ├── deps.py            # RuntimeDeps container
│   │   └── app.py             # FastAPI app factory with lifespan
│   ├── runtime/                # Runtime Layer (all business logic)
│   │   ├── events/             # EventBus, backpressure, event models
│   │   ├── registry/           # Capability Registry
│   │   ├── discovery/          # Bootstrap discovery procedure
│   │   ├── health/             # Health Monitor (continuous re-probing)
│   │   ├── mode/               # Operational mode evaluation
│   │   ├── secrets/            # Secret storage + redaction
│   │   ├── budget/             # Per-session token budget
│   │   ├── router/             # Model Router + Circuit Breaker + Backoff
│   │   ├── session/            # Session CRUD
│   │   ├── audit/              # Audit Trail (event projection)
│   │   ├── classifier/         # Deterministic intent classifier + router
│   │   ├── clarification/      # SessionContext + question workflow
│   │   ├── specification/      # Architect role invocation
│   │   ├── planner/            # Task dependency DAG
│   │   ├── workspace/          # Workspace Manager (sandboxed copies)
│   │   ├── dispatcher/         # Task Dispatcher (sequential execution)
│   │   ├── verification/       # Verification Pipeline (advisory + blocking)
│   │   ├── policies/           # Policy Engine (retry/escalate/skip)
│   │   ├── commit/             # Commit workflow
│   │   ├── finalization/       # Push + build summary
│   │   ├── documentation/      # Digital Twin diff + doc maintenance
│   │   ├── inspector/          # Runtime Inspector (query-only facade)
│   │   ├── recovery/           # Crash recovery + checkpointing
│   │   ├── interrupt/          # Pause/resume/redirect/stop
│   │   ├── learning/           # Outcome recording + recommendations
│   │   ├── boundaries/         # Layer boundary enforcement
│   │   ├── models.py           # Core domain models
│   │   ├── protocols.py        # Plugin protocol interfaces
│   │   └── types.py            # Shared type definitions
│   ├── db/                     # Infrastructure Layer (DB stubs)
│   └── config/                 # Infrastructure Layer (config stubs)
├── config/                     # YAML configuration files
│   ├── models.yaml             # AI provider chains per role
│   ├── policies.yaml           # Retry/escalation rules
│   ├── rate_limits.yaml        # Token/cost limits
│   ├── tools.yaml              # Coding tool configuration
│   └── verification.yaml       # Verifier stage definitions
├── tests/                      # 1,191 tests (unit + property-based)
├── .env.example                # Environment variable template
└── pyproject.toml              # Project metadata + dependencies
```

## Quick Start

### Prerequisites

- Python 3.11+
- pip or uv

### Setup

```bash
cd backend

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# or: .venv\Scripts\activate  # Windows

# Install dependencies
pip install -e ".[dev]"

# Copy environment template
cp .env.example .env
# Edit .env with your API keys
```

### Run Tests

```bash
cd backend
pytest
```

All 1,240+ tests should pass in ~5 minutes. Property-based tests use Hypothesis with 100-500 examples each.

### Start the Server

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Invoke a Build

```bash
curl -X POST http://localhost:8000/workflow/invoke \
  -H "Content-Type: application/json" \
  -d '{"message": "Add user authentication with JWT", "session_id": "my-session"}'
```

The response includes the workflow status, commit SHAs, node path traversed, and any errors.

### Run Specific Test Groups

```bash
# Event bus and core
pytest tests/test_event_bus.py tests/test_backpressure.py

# Model router + circuit breaker
pytest tests/test_router.py tests/test_circuit_breaker.py tests/test_router_events.py

# API endpoints
pytest tests/test_api.py tests/test_auth.py

# Property-based tests only
pytest tests/test_event_properties.py tests/test_discovery_properties.py \
       tests/test_secret_properties.py tests/test_budget_properties.py \
       tests/test_router_properties.py tests/test_audit_properties.py \
       tests/test_planner_properties.py tests/test_workspace_properties.py \
       tests/test_verification_properties.py tests/test_inspector_properties.py
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/workflow/invoke` | **Run a full build workflow** |
| GET | `/health` | Health check |
| POST | `/sessions` | Create a build session |
| GET | `/sessions` | List all sessions |
| GET | `/sessions/{id}` | Get session detail |
| DELETE | `/sessions/{id}` | Delete session |
| POST | `/sessions/{id}/messages` | Send developer message |
| GET | `/sessions/{id}/artifacts/spec` | Get specification |
| POST | `/sessions/{id}/interrupt` | Pause build |
| POST | `/sessions/{id}/resume` | Resume build |
| POST | `/sessions/{id}/redirect` | Change direction |
| POST | `/sessions/{id}/stop` | Stop build |
| GET | `/sessions/{id}/status` | Runtime status |
| GET | `/sessions/{id}/explain` | Last decision |
| GET | `/capabilities` | Capability summary |
| WS | `/sessions/{id}/events` | Event stream |

All endpoints require `Authorization: Bearer <token>` (configurable via `FORGE_API_TOKEN`).

## Configuration

Configuration lives in `backend/config/` as YAML files:

- **models.yaml** — Maps roles (Coder, Reviewer, Architect, etc.) to ordered provider+model fallback chains
- **policies.yaml** — Retry budget, escalation thresholds, and per-stage rules
- **rate_limits.yaml** — Per-session token/cost limits
- **tools.yaml** — Coding tool enablement and configuration
- **verification.yaml** — Advisory and blocking verifier stage definitions

## Key Design Decisions

1. **Event Bus as spine** — Single source of truth. Audit trail, WebSocket broadcaster, and learning engine are all subscribers.
2. **In-memory stores for v1** — All persistence uses dicts/lists. PostgreSQL integration is the next step.
3. **Sequential task execution** — Parallelism seam exists but v1 runs one task at a time.
4. **Deterministic fast-path classifier** — "stop", "pause", "status" never depend on an AI model being reachable.
5. **Circuit breaker per provider** — Dead providers ejected in milliseconds, not after a 40s backoff ladder.
6. **Redaction at every boundary** — Secrets never appear in audit logs, state snapshots, or events.

## Requirements Coverage

All 26 requirements from the spec are implemented:

- Req 1–3: Session lifecycle, intent classification, clarification
- Req 4–5: Specification generation, task planning
- Req 6–8: Workspace isolation, verification pipeline, policy engine
- Req 9–10: Commit workflow, documentation maintenance
- Req 11–12: Model routing with fallback, budget governance
- Req 13–16: Discovery, health monitoring, registry, operational mode
- Req 17–18: Event bus ordering/causality, streaming backpressure
- Req 19–21: Audit trail, runtime inspector, crash recovery
- Req 22: Secret handling
- Req 23–24: Layer boundaries, interrupt handling
- Req 25–26: Learning recorder, API endpoints

## Correctness Properties (Hypothesis)

12 properties validated with property-based testing:

1. Routing soundness
2. Discovery soundness
3. Verification merge order-independence
4. Event ordering
5. Causality closure / Plan acyclicity
6. Audit replay fidelity
7. Router fallback monotonicity
8. Budget safety / Explainability without inference
9. Secret non-leakage / Workspace isolation
10. Documentation non-drift

## License

Private — all rights reserved.
