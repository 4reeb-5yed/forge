# Architecture

## 6-Layer Design

Forge uses a strict 6-layer architecture. Each layer communicates only with its adjacent layer through defined interfaces.

```mermaid
graph TB
    L1[Layer 1: Frontend<br/>Next.js + Tailwind]
    L2[Layer 2: Application<br/>FastAPI REST + WebSocket]
    L3[Layer 3: Workflow<br/>LangGraph State Machine]
    L4[Layer 4: Runtime<br/>27 asyncio modules]
    L5[Layer 5: Adapter<br/>OpenRouter, GitHub, Aider]
    L6[Layer 6: Infrastructure<br/>PostgreSQL, Docker, GitHub API]

    L1 --> L2
    L2 --> L3
    L3 --> L4
    L4 --> L5
    L5 --> L6
```

## Layer Responsibilities

### Layer 1: Frontend

| Aspect | Detail |
|--------|--------|
| **Location** | `frontend/` |
| **Technology** | Next.js 14, React 18, Tailwind CSS, TypeScript |
| **Responsibility** | Render chat, session list, event log, status bar |
| **Boundary rule** | Knows nothing about models, tools, or orchestration |
| **Communicates with** | Application layer via REST + WebSocket |

### Layer 2: Application

| Aspect | Detail |
|--------|--------|
| **Location** | `backend/app/api/` |
| **Technology** | FastAPI, Pydantic, uvicorn |
| **Responsibility** | HTTP/WS boundary, session CRUD, event broadcasting, auth |
| **Boundary rule** | Contains zero engineering logic — pure transport translation |
| **Communicates with** | Workflow layer (invocation), Runtime (session, inspector, interrupt) |

### Layer 3: Workflow

| Aspect | Detail |
|--------|--------|
| **Location** | `backend/app/workflow/` |
| **Technology** | LangGraph StateGraph |
| **Responsibility** | Orchestrate the build lifecycle as a state machine |
| **Boundary rule** | Nodes are thin wrappers that delegate to runtime components |
| **Communicates with** | Runtime layer (all 27 modules via RuntimeDeps) |

### Layer 4: Runtime

| Aspect | Detail |
|--------|--------|
| **Location** | `backend/app/runtime/` |
| **Technology** | Pure Python asyncio, Protocol interfaces |
| **Responsibility** | All state, all events, all decisions |
| **Boundary rule** | Contains zero HTTP/transport logic. Depends on adapters via protocols only |
| **Communicates with** | Adapter layer (via protocol interfaces) |

### Layer 5: Adapter

| Aspect | Detail |
|--------|--------|
| **Location** | `backend/app/adapters/` |
| **Technology** | httpx, asyncio subprocess |
| **Responsibility** | Translate one protocol call into one infrastructure call |
| **Boundary rule** | Contains zero business logic. No back-imports to runtime (except via shared types) |
| **Communicates with** | Infrastructure (HTTP APIs, CLIs, databases) |
| **Shared types** | Imports `Health`, `ToolResult`, `PermanentError` from `app/shared/` |

### Layer 6: Infrastructure

| Aspect | Detail |
|--------|--------|
| **Technology** | PostgreSQL, Docker, GitHub API, OpenRouter API, Aider CLI |
| **Responsibility** | External services not controlled by Forge code |
| **Boundary rule** | Only accessed through adapters |

## Communication Rules

```
✅ Layer N can call Layer N+1 (downward only)
✅ Layer N can return data to Layer N-1 (upward via return values)
❌ Layer N cannot skip layers (e.g., API cannot call Adapter directly)
❌ Adapter cannot import from Runtime (no back-imports)
❌ Runtime cannot import from API (no transport awareness)
```

### Shared Layer Exception

The `app/shared/` module contains types shared across layers (Health, ToolResult, PermanentError). Both Adapters and Runtime can import from Shared. This avoids circular dependencies while maintaining clean boundaries.

```
✅ Adapter can import from Shared (for Health, ToolResult types)
✅ Runtime can import from Shared (for shared types)
❌ Shared cannot import from Adapter or Runtime (no back-imports)
```

The boundary checker (`app/boundaries.py`) enforces these rules at test time:

```python
from app.boundaries import check_all_boundaries
violations = check_all_boundaries(app_root)  # Returns list of BoundaryViolation
```

## The Event Bus — Architectural Spine

The Event Bus is the single source of truth for everything that happens in Forge.

```mermaid
graph LR
    C1[Component A] -->|emit Event| EB[Event Bus]
    C2[Component B] -->|emit Event| EB
    C3[Component C] -->|emit Event| EB
    EB -->|subscribe| AT[Audit Trail]
    EB -->|subscribe| WS[WebSocket Broadcaster]
    EB -->|subscribe| LE[Learning Engine]
    EB -->|subscribe| RI[Runtime Inspector<br/>reads projections]
```

### Event Structure

Every event in the system follows this schema:

```python
@dataclass
class Event:
    schema_version: str       # "1.0"
    seq: int                  # Monotonically increasing per session
    session_id: str           # Which session produced this event
    type: EventType           # Typed enum (TASK_START, FORGE_READY, etc.)
    timestamp: datetime       # When the event was created
    source: str               # Which component emitted it
    payload: dict[str, Any]   # Event-specific data
    event_id: str             # Unique event identifier
    causation_id: str | None  # What caused this event
    correlation_id: str       # Trace across related events
```

### Why the Event Bus Matters

1. **Audit trail** is a projection of events — never constructed from state
2. **WebSocket streaming** to the frontend is just another subscriber
3. **Learning engine** records outcomes by observing events, not by coupling to execution
4. **Inspector** queries the audit trail (never the components directly)
5. **Crash recovery** replays from the last checkpointed event sequence

## Component Dependency Graph

```mermaid
graph TD
    EB[EventBus] --> AT[AuditTrail]
    EB --> WS[WebSocket]
    EB --> LE[LearningRecorder]
    
    REG[Registry] --> HM[HealthMonitor]
    REG --> MR[ModelRouter]
    REG --> ME[ModeEvaluator]
    
    MR --> DS[Discovery]
    
    PE[PolicyEngine] --> VE[Verification]
    PE --> TD[TaskDispatcher]
    
    SM[SessionManager] --> SH[SecretHolder]
    
    WM[WorkspaceManager] --> EB
    CR[CrashRecovery] --> EB
    IH[InterruptHandler] --> EB
    RI[RuntimeInspector] --> AT
    RI --> REG
```

## Data Flow: Complete Build Lifecycle

```mermaid
sequenceDiagram
    participant U as User
    participant API as Application Layer
    participant WF as Workflow (LangGraph)
    participant IN as Intake Node
    participant CL as Classify Node
    participant CLR as Clarify Node
    participant AR as Architect Node
    participant PL as Plan Node
    participant EX as Execute Node
    participant VR as Verify Node
    participant PO as Policy Node
    participant CM as Commit Node
    participant DU as DocUpdate Node
    participant FN as Finalize Node

    U->>API: POST /workflow/invoke
    API->>WF: invoke(ForgeState)
    WF->>IN: intake(state)
    IN-->>WF: {session_id, message}
    WF->>CL: classify(state)
    CL-->>WF: {intent: "build_intent"}
    WF->>CLR: clarify(state)
    CLR-->>WF: {goals, constraints}
    WF->>AR: architect(state)
    AR-->>WF: {digital_twin, spec_artifact_uri}
    WF->>PL: plan(state)
    PL-->>WF: {tasks, task_ordering}
    
    loop For each task in ordering
        WF->>EX: execute(state)
        EX-->>WF: {workspace results}
        WF->>VR: verify(state)
        alt Verification passed
            VR-->>WF: {passed: true}
            WF->>CM: commit(state)
            CM-->>WF: {commit_sha}
        else Verification failed
            VR-->>WF: {passed: false}
            WF->>PO: policy(state)
            PO-->>WF: {retry/skip/escalate}
            WF->>EX: execute(state) again
        end
    end
    
    WF->>DU: doc_update(state)
    DU-->>WF: {doc_updates}
    WF->>FN: finalize(state)
    FN-->>WF: {status: "completed"}
    WF-->>API: Final ForgeState
    API-->>U: {status, commit_shas, node_path}
```

## Concurrency Model

- **Single-process asyncio core** (v1) — no distributed workers yet
- **Blocking work** (git clone, Aider subprocess) runs in thread/process executors
- **Sequential task execution** with a parallelism seam ready for future expansion
- **Per-session locks** for event ordering guarantees
- **Circuit breakers** with cancellable backoff for transient provider failures
