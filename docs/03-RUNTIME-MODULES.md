# Runtime Modules

The runtime layer (`backend/app/runtime/`) contains 30+ modules organized by concern. Each module follows the same structural pattern:

- One module = one responsibility
- Protocol interfaces for dependencies (dependency injection)
- Every state-changing operation emits a typed event
- Custom exception classes with context (session_id, task_id, etc.)

---

## Events

### `events/` — Event Bus & Event Models

**Purpose:** Ordered, typed event delivery. The architectural spine of Forge.

**Key Classes:**
- `EventBus` — Pub/sub with wildcard and topic-based subscriptions
- `Event` — Immutable event dataclass with schema_version, seq, type, payload
- `EventType` — Enum of all event types (TASK_START, FORGE_READY, etc.)

**Public API:**
```python
class EventBus:
    async def publish(event: Event) -> None
    def subscribe(topic: str, handler: Callable, subscriber_id: str) -> None
    def unsubscribe(subscriber_id: str) -> None
```

**Dependencies:** None (leaf component)

**Events emitted:** N/A (it *is* the event transport)

---

## Registry & Discovery

### `registry/` — Capability Registry

**Purpose:** Track what capabilities are available at runtime. Single source of truth for "what can I use right now?"

**Key Classes:**
- `CapabilityRegistry` — Thread-safe registry of capability entries

**Public API:**
```python
class CapabilityRegistry:
    async def register(entry: CapabilityEntry) -> None
    async def deregister(name: Capability) -> None
    def has_by_name(name: Capability) -> bool
    def has_by_kind(kind: CapabilityKind) -> bool
    def get_all() -> list[CapabilityEntry]
    def get_by_kind(kind: CapabilityKind) -> list[CapabilityEntry]
```

**Dependencies:** EventBus (emits registration events)

**Events emitted:** `CAPABILITY_REGISTERED`, `CAPABILITY_DEREGISTERED`

---

### `discovery/` — Resource Discovery

**Purpose:** At boot time, probe all configured resources and register healthy ones in the Registry.

**Key Classes:**
- `run_discovery()` — Async function that loads config, probes, and registers

**Public API:**
```python
async def run_discovery(
    config_dir: Path,
    registry: CapabilityRegistry,
    probe_map: dict[str, Callable],
    event_emitter: Callable,
    session_id: str,
) -> None
```

**Dependencies:** Registry, config YAML files, probe functions

**Events emitted:** `DISCOVERY_START`, `DISCOVERY_COMPLETE`, `PROBE_SUCCESS`, `PROBE_FAILURE`

---

### `health/` — Health Monitor

**Purpose:** Continuously re-check registered capabilities. Eject unhealthy providers, re-admit recovered ones.

**Key Classes:**
- `HealthMonitor` — Background task that periodically probes capabilities
- `HealthMonitorConfig` — Configuration (interval, timeout)

**Public API:**
```python
class HealthMonitor:
    async def start() -> None
    async def stop() -> None
    async def check_now() -> dict[Capability, Health]
```

**Dependencies:** Registry, probe_map, EventBus

**Events emitted:** `HEALTH_CHECK_START`, `HEALTH_CHECK_COMPLETE`, `CAPABILITY_UNHEALTHY`, `CAPABILITY_RECOVERED`

---

## AI Routing

### `router/` — Model Router

**Purpose:** Resolve abstract AI roles (architect, planner, coder) to concrete providers with fallback chains.

**Key Classes:**
- `ModelRouter` — Role-to-provider resolution with circuit breakers
- `RoleChainConfig` — Maps each Role to an ordered list of provider/model pairs
- `PermanentError` — Non-retryable error (auth failure, model not found)

**Public API:**
```python
class ModelRouter:
    async def route(role: Role, messages: list, session_id: str, **kwargs) -> str
    def get_chain(role: Role) -> list[tuple[str, str]]
```

**Dependencies:** Registry (checks provider health), call_adapter (protocol), EventBus

**Events emitted:** `AI_CALL_START`, `AI_CALL_SUCCESS`, `AI_CALL_FAILURE`, `PROVIDER_CIRCUIT_OPEN`

---

### `classifier/` — Intent Classifier

**Purpose:** Deterministic classification of user messages into intents (build, status, interrupt, natural language).

**Key Classes:**
- `IntentClassifier` — Rule-based classifier with keyword matching

**Public API:**
```python
class IntentClassifier:
    def classify(message: str) -> str  # Returns intent string
```

**Dependencies:** None

**Events emitted:** None (pure function)

---

## Session & Audit

### `session/` — Session Manager

**Purpose:** Create, retrieve, update, and delete build sessions.

**Key Classes:**
- `SessionManager` — CRUD for sessions with secret redaction

**Public API:**
```python
class SessionManager:
    async def create(repo_url: str, goal: str, build_mode: str) -> Session
    async def get(session_id: str) -> Session
    async def list() -> list[Session]
    async def update(session_id: str, updates: dict) -> Session
    async def delete(session_id: str) -> bool
```

**Dependencies:** SecretHolder

**Events emitted:** `SESSION_CREATED`, `SESSION_UPDATED`, `SESSION_DELETED`

---

### `audit/` — Audit Trail

**Purpose:** Persisted projection of all events for a session. Provides the "what happened and why" record.

**Key Classes:**
- `AuditTrail` — Subscribes to EventBus, stores events in order

**Public API:**
```python
class AuditTrail:
    async def handle_event(event: Event) -> None
    async def get_events(session_id: str, since_seq: int = 0) -> list[Event]
    async def get_decisions(session_id: str) -> list[Event]
    async def get_last_decision(session_id: str) -> Event | None
```

**Dependencies:** EventBus (subscribes to `*`)

**Events emitted:** None (it is a subscriber/projection)

---

### `secrets/` — Secret Holder

**Purpose:** In-memory-only storage for sensitive values. Redacts at every serialization boundary.

**Key Classes:**
- `SecretHolder` — Per-session secret storage with redaction

**Public API:**
```python
class SecretHolder:
    def store(session_id: str, key: str, value: str) -> None
    def retrieve(session_id: str, key: str) -> str | None
    def redact(data: dict) -> dict
    def redact_or_raise(data: dict) -> dict  # Raises if raw secret survives
```

**Dependencies:** None

**Events emitted:** None

---

### `inspector/` — Runtime Inspector

**Purpose:** Query-only interface for "what is happening?" and "why was that decision made?" — answers without AI.

**Key Classes:**
- `RuntimeInspector` — Read-only queries against audit trail and registry

**Public API:**
```python
class RuntimeInspector:
    async def get_status(session_id: str) -> RuntimeStatus
    async def explain_last_decision(session_id: str) -> DecisionExplanation
    def get_capabilities() -> CapabilitySummary
```

**Dependencies:** AuditTrail, Registry

**Events emitted:** None (read-only)

---

## Workflow Logic

### `clarification/` — Clarification Engine

**Purpose:** Generate clarifying questions for ambiguous goals before building.

**Key Classes:**
- `ClarificationEngine` — Produces questions from goal + constraints

**Public API:**
```python
class ClarificationEngine:
    async def clarify(goal: str, context: SessionContext) -> SessionContext
```

**Dependencies:** ModelRouter (for AI-powered clarification)

**Events emitted:** `CLARIFICATION_START`, `CLARIFICATION_COMPLETE`

---

### `specification/` — Specification Generator

**Purpose:** Generate a structured specification artifact from clarified requirements.

**Key Classes:**
- `SpecificationGenerator` — Produces spec from architect output

**Public API:**
```python
class SpecificationGenerator:
    async def generate(twin: DigitalTwin, context: SessionContext) -> str
```

**Dependencies:** ModelRouter

**Events emitted:** `SPEC_GENERATED`

---

### `planner/` — Task Planner

**Purpose:** Break a specification into a topologically-ordered task graph.

**Key Classes:**
- `TaskPlanner` — Generates tasks with dependencies and target files

**Public API:**
```python
class TaskPlanner:
    async def plan(spec: str, twin: DigitalTwin) -> list[Task]
    def topological_sort(tasks: list[Task]) -> list[str]
```

**Dependencies:** ModelRouter

**Events emitted:** `PLAN_START`, `PLAN_COMPLETE`

---

### `dispatcher/` — Task Dispatcher

**Purpose:** Assign tasks to workspaces and coding tools for execution.

**Key Classes:**
- `TaskDispatcher` — Picks the next task, assigns workspace and tool

**Public API:**
```python
class TaskDispatcher:
    async def dispatch(task: Task, workspace_id: str) -> DispatchResult
    def next_task(state: ForgeState) -> Task | None
```

**Dependencies:** WorkspaceManager, Registry, PolicyEngine

**Events emitted:** `TASK_DISPATCHED`, `TASK_START`

---

### `policies/` — Policy Engine

**Purpose:** Decide what to do when things fail: retry, skip, or escalate.

**Key Classes:**
- `PolicyEngine` — Rule-based decision engine
- `PolicyConfig` — Retry budgets, escalation rules

**Public API:**
```python
class PolicyEngine:
    def decide(task: Task, failure_context: dict) -> PolicyDecision
    # PolicyDecision: retry | skip | escalate | abort
```

**Dependencies:** Configuration (policies.yaml)

**Events emitted:** `POLICY_DECISION`

---

### `mode/` — Mode Evaluator

**Purpose:** Determine OPERATIONAL vs DEGRADED mode based on available capabilities.

**Key Classes:**
- `ModeEvaluator` — Evaluates registry against required capabilities

**Public API:**
```python
class ModeEvaluator:
    async def evaluate_and_emit() -> ModeResult
    def current_mode() -> OperationalMode
```

**Dependencies:** Registry, EventBus

**Events emitted:** `MODE_CHANGE`, `MODE_EVALUATED`

---

## Execution

### `workspace/` — Workspace Manager

**Purpose:** Create, manage, and destroy isolated sandboxes for task execution.

**Key Classes:**
- `WorkspaceManager` — Lifecycle management of workspace directories

**Public API:**
```python
class WorkspaceManager:
    async def create(session_id: str, task_id: str) -> Workspace
    async def destroy(workspace_id: str) -> None
    def get(workspace_id: str) -> Workspace | None
```

**Dependencies:** EventBus, filesystem

**Events emitted:** `WORKSPACE_CREATED`, `WORKSPACE_DESTROYED`

---

### `commit/` — Commit Handler

**Purpose:** Orchestrate the commit of verified work back to the repository.

**Key Classes:**
- `CommitHandler` — Stage, commit, and optionally push

**Public API:**
```python
class CommitHandler:
    async def commit(workspace_path: str, message: str) -> str  # Returns SHA
    async def push(workspace_path: str) -> None
```

**Dependencies:** VCS adapter (via protocol)

**Events emitted:** `COMMIT_START`, `COMMIT_COMPLETE`, `PUSH_COMPLETE`

---

### `documentation/` — Documentation Manager

**Purpose:** Track and update documentation as a first-class artifact (not a side effect).

**Key Classes:**
- `DocumentationManager` — Tracks doc files, detects drift, generates updates

**Public API:**
```python
class DocumentationManager:
    async def detect_drift(twin: DigitalTwin) -> list[DocFileEntry]
    async def generate_update(task: Task, changes: dict) -> str
```

**Dependencies:** ModelRouter, DigitalTwin

**Events emitted:** `DOC_DRIFT_DETECTED`, `DOC_UPDATE_COMPLETE`

---

## Verification

### `verification/` — Verification Pipeline

**Purpose:** Run verification stages (lint, test, type-check) against task output.

**Key Classes:**
- `VerificationPipeline` — Ordered execution of verifier stages
- `VerificationResult` — Pass/fail with details per stage

**Public API:**
```python
class VerificationPipeline:
    async def verify(workspace_path: str, task: Task) -> VerificationResult
    def add_stage(stage: VerifierStage) -> None
```

**Dependencies:** Registry (discovers available verifiers)

**Events emitted:** `VERIFICATION_START`, `VERIFICATION_STAGE_PASS`, `VERIFICATION_STAGE_FAIL`, `VERIFICATION_COMPLETE`

---

## Observability

### `budget/` — Session Budget

**Purpose:** Token and cost governance per session. Enforces limits, tracks usage.

**Key Classes:**
- `SessionBudget` — Per-session token/cost tracker

**Public API:**
```python
class SessionBudget:
    def consume(tokens: int, cost: float = 0.0) -> None
    def check(tokens: int) -> bool  # Would this exceed budget?
    def remaining() -> int
    def usage_report() -> dict
```

**Dependencies:** None

**Events emitted:** `BUDGET_WARNING`, `BUDGET_EXCEEDED`

---

### `learning/` — Learning Recorder

**Purpose:** Record task outcomes (success/failure patterns) for future improvement.

**Key Classes:**
- `LearningRecorder` — Subscribes to events, records outcomes

**Public API:**
```python
class LearningRecorder:
    async def record(session_id: str, task_id: str, outcome: str, data: dict) -> None
    async def get_outcomes(session_id: str) -> list[dict]
```

**Dependencies:** EventBus (subscribes)

**Events emitted:** `LEARNING_RECORDED`

---

### `recovery/` — Crash Recovery

**Purpose:** Resume interrupted builds from the last checkpoint.

**Key Classes:**
- `CrashRecovery` — Checkpoint writing and replay

**Public API:**
```python
class CrashRecovery:
    async def checkpoint(session_id: str, node_id: str, seq: int, state: dict) -> None
    async def recover(session_id: str) -> ForgeState | None
    async def list_recoverable() -> list[str]
```

**Dependencies:** Checkpoint store (protocol), EventBus

**Events emitted:** `CHECKPOINT_WRITTEN`, `RECOVERY_START`, `RECOVERY_COMPLETE`

---

### `interrupt/` — Interrupt Handler

**Purpose:** Handle pause, resume, redirect, and stop signals within 2 seconds.

**Key Classes:**
- `InterruptHandler` — Signal processing for build control

**Public API:**
```python
class InterruptHandler:
    async def interrupt(session_id: str, signal: str) -> InterruptResult
    def is_interrupted(session_id: str) -> bool
```

**Dependencies:** EventBus

**Events emitted:** `INTERRUPT_RECEIVED`, `SESSION_PAUSED`, `SESSION_RESUMED`, `SESSION_STOPPED`

---

### `finalization/` — Finalization

**Purpose:** Clean up after a build completes — close workspace, finalize session, emit summary.

**Key Classes:**
- `Finalizer` — End-of-build cleanup and summary

**Public API:**
```python
class Finalizer:
    async def finalize(state: ForgeState) -> ForgeState
```

**Dependencies:** WorkspaceManager, SessionManager, EventBus

**Events emitted:** `SESSION_FINALIZED`

---

### `boundaries/` — Boundary Checker

**Purpose:** Enforce layer boundary rules at test time. Prevents architectural drift.

**Key Classes:**
- `BoundaryChecker` — Static analysis of import graphs

**Public API:**
```python
def enforce_boundaries() -> None  # Raises BoundaryCheckError on violation
```

**Dependencies:** None (static analysis)

**Events emitted:** None

---

## Shared (`app/shared/`)

The `app/shared/` module is the canonical source for types shared across layers:

### `__init__.py` — Shared Types

| Type | Description |
|------|-------------|
| `Health` / `HealthStatus` | Health check result type used by all adapters |
| `ToolResult` | Result of coding tool execution |
| `PermanentError` | Non-retryable error for auth failures |

**Note:** `app/runtime/types.py` re-exports these types for backward compatibility.

---

## Production Modules

These modules provide production-ready features for enterprise deployments.

### `persistence.py` — PostgreSQL Persistence Layer

**Purpose:** Database-backed implementations of runtime stores.

**Key Classes:**
- `PostgresSessionStore` — PostgreSQL session persistence
- `PostgresAuditStore` — PostgreSQL audit persistence
- `PostgresCheckpointStore` — PostgreSQL checkpoint persistence
- `PostgresLearningStore` — PostgreSQL learning outcome persistence

**Public API:**
```python
async def init_persistence(database_url: str, pool_size: int = 10) -> bool
async def close_persistence() -> None
def create_persistence_stores(pool) -> tuple[...PostgresXxxStore]
```

**Dependencies:** asyncpg, db/pool.py

**Notes:** Auto-wired during bootstrap when `DATABASE_URL` is available. Falls back to in-memory stores for development.

---

### `approval.py` — Approval Gates

**Purpose:** Human-in-the-loop approval before commits.

**Key Classes:**
- `ApprovalManager` — Manage approval requests and decisions
- `ApprovalRequest` — Pending approval request
- `ApprovalResult` — Decision result
- `ApprovalStatus` — Enum: PENDING, APPROVED, REJECTED, EXPIRED, CANCELLED
- `ApprovalType` — Enum: PRE_COMMIT, PRE_PUSH, PRE_TASK

**Public API:**
```python
class ApprovalManager:
    async def create_request(...) -> ApprovalRequest
    async def wait_for_decision(...) -> ApprovalResult
    async def approve(request_id, reviewer, comment) -> ApprovalResult
    async def reject(request_id, reviewer, comment) -> ApprovalResult
    def get_pending_requests(session_id) -> list[ApprovalRequest]
```

**Dependencies:** EventBus (emits approval events)

**Events emitted:** `APPROVAL_REQUESTED`, `APPROVAL_APPROVED`, `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`

---

### `scheduler.py` — Session Scheduler

**Purpose:** Concurrent build execution with priority-based scheduling.

**Key Classes:**
- `SessionScheduler` — Priority queue for parallel session execution
- `QueuedSession` — Queued session with metadata
- `SchedulerStatus` — Enum: IDLE, RUNNING, PAUSED, STOPPED
- `SessionStatus` — Enum: QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED

**Public API:**
```python
class SessionScheduler:
    async def enqueue(session_id, session_data, priority=0) -> None
    async def start() -> None
    async def stop() -> None
    async def pause() -> None
    async def resume() -> None
    def get_running_sessions() -> list[str]
```

**Dependencies:** asyncio

**Environment Variables:**
- `FORGE_MAX_CONCURRENT` — Maximum concurrent sessions (default: 3)

---

### `build_timeout.py` — Build Timeout Manager

**Purpose:** Auto-stop builds exceeding configurable timeout.

**Key Classes:**
- `BuildTimeoutManager` — Track and auto-stop long-running builds
- `BuildTimeout` — Timeout state for a session

**Public API:**
```python
class BuildTimeoutManager:
    async def start_tracking(session_id, timeout_seconds=None) -> None
    async def stop_tracking(session_id) -> None
    async def extend_timeout(session_id, additional_seconds) -> bool
    async def get_remaining_time(session_id) -> int | None
```

**Dependencies:** asyncio, InterruptHandler (optional)

**Environment Variables:**
- `FORGE_BUILD_TIMEOUT_SECONDS` — Default timeout (default: 1800 = 30 minutes)

**Events emitted:** `BUILD_TIMEOUT_STARTED`, `BUILD_TIMEOUT_EXCEEDED`

---

### `learning_engine.py` — Learning Engine

**Purpose:** Analyze failure patterns and generate recommendations.

**Key Classes:**
- `LearningEngine` — Pattern analysis and recommendations
- `LearningPattern` — Identified pattern from outcomes
- `ModelPerformance` — Model performance metrics
- `ProviderHealth` — Provider health metrics
- `LearningRecommendations` — Generated recommendations

**Public API:**
```python
class LearningEngine:
    async def analyze() -> LearningRecommendations
    def _analyze_model_performance(outcomes) -> list[dict]
    def _analyze_provider_health(outcomes) -> list[dict]
    def _analyze_task_patterns(outcomes) -> list[dict]
```

**Dependencies:** LearningRecorder, datetime

**Environment Variables:**
- `FORGE_LEARNING_WINDOW_DAYS` — Analysis window (default: 7)

---

### `stream_router.py` — Stream Router

**Purpose:** Real-time token streaming for AI completions.

**Key Classes:**
- `StreamRouter` — Streaming wrapper for AI responses

**Public API:**
```python
class StreamRouter:
    async def route_stream(role, messages, session_id) -> AsyncIterator[dict]
```

**Dependencies:** ModelRouter, EventBus

**Notes:** Wraps the ModelRouter to emit TOKEN events for real-time frontend display.

---

### Workflow (`workflow/checkpoint_middleware.py`) — Checkpoint Middleware

**Purpose:** Automatic workflow state persistence for crash recovery.

**Key Classes:**
- `CheckpointMiddleware` — Wrap workflow nodes for auto-checkpointing

**Public API:**
```python
class CheckpointMiddleware:
    def wrap_node(node_name, node_fn) -> Callable
    async def checkpoint(session_id, node_id, state) -> None
    async def recover(session_id) -> dict | None
    async def list_recoverable_sessions() -> list[dict]
```

**Dependencies:** db/checkpoint_store

**Environment Variables:**
- `FORGE_CHECKPOINT_INTERVAL` — Seconds between checkpoints (default: 60)

**Notes:** Automatically checkpoints after each node execution. Redacts sensitive data (tokens, secrets).
