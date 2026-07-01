# Runtime Modules

The runtime layer (`backend/app/runtime/`) contains 27 modules organized by concern. Each module follows the same structural pattern:

- One module = one responsibility
- Dependencies are injected as plain callables/objects (no framework DI container)
- State-changing operations emit a typed `Event` from the closed `EventType` catalog (some modules — `session/`, `planner/` — are pure and emit nothing)
- Custom exception classes carry context (session_id, task_id, etc.)

Not every module exposes a class. Several are plain modules of functions (`classifier/`, `clarification/`, `planner/`, `boundaries/`). This document reflects exactly what's in code, not an idealized shape.

---

## Events

### `events/` — Event Bus & Event Models

**Purpose:** Ordered, typed event delivery with backpressure. The architectural spine of Forge.

**Key files:** `models.py` (Event, EventType, DecisionRecord), `bus.py` (EventBus), `backpressure.py` (BackpressureQueue).

**Key classes:**
- `Event` — Immutable frozen dataclass: `schema_version`, `seq`, `session_id`, `type`, `timestamp`, `source`, `payload`, `causation_id`, `correlation_id`, `event_id`. Has a static factory `Event.create(...)`.
- `EventType` — Closed `str` Enum. The real catalog (grouped as declared in `events/models.py`):
  - Model routing: `MODEL_SELECTED`, `MODEL_FALLBACK`
  - Capability lifecycle: `CAPABILITY_REGISTERED`, `CAPABILITY_DEREGISTERED`, `CAPABILITY_DEGRADED`, `CAPABILITY_RECOVERED`
  - Workspace lifecycle: `WORKSPACE_CREATED`, `WORKSPACE_DESTROYED`
  - Verification: `VERIFY_STAGE`, `VERIFY_PASSED`
  - Policy: `POLICY_DECISION`
  - Task lifecycle: `TASK_START`, `TASK_DONE`, `TASK_FAIL`
  - Build lifecycle: `BUILD_DONE`
  - Specification/planning: `SPEC_READY`, `TASKS_READY`
  - VCS: `COMMIT_DONE`, `COMMIT_FAILED`
  - Approval gates: `APPROVAL_PENDING`, `APPROVAL_REJECTED`
  - Errors: `CONFIG_ERROR`, `RUNTIME_ERROR`, `WORKFLOW_ERROR`
  - Documentation: `DOC_UPDATED`, `DOC_DRIFT`
  - Boot/readiness: `FORGE_BOOT_DISCOVERY_COMPLETE`, `FORGE_READY`
  - Interaction: `QUESTION`, `ERROR`, `TOKEN`
  - Budget: `BUDGET_EXCEEDED`
  - Interrupt lifecycle: `INTERRUPT_PAUSED`, `INTERRUPT_RESUMED`, `INTERRUPT_STOPPED`, `INTERRUPT_REDIRECTED`
- `DecisionKind` — Enum of decision kinds recorded in the audit trail (`RETRY`, `ESCALATE`, `SKIP`, `MODEL_FALLBACK`, `CAPABILITY_TRANSITION`, `APPROVAL_GATE`, `TASK_FAILED`, `INTENT_CLASSIFICATION`, `CLARIFICATION`, `MODEL_SELECTION`, `TOOL_SELECTION`, `POLICY_APPLICATION`, `VERIFICATION_OUTCOME`, `TASK_OUTCOME`).
- `DecisionRecord` — Frozen dataclass backing explainability (`kind`, `subject`, `inputs`, `decision`, `rationale`, `alternatives`, `causing_event_seq`, `decision_id`, `session_id`, `timestamp`). Rationale is always template-built from inputs, never AI-generated.

**Public API:**
```python
class EventBus:
    def __init__(self, max_queue_depth: int = 1000, lifecycle_timeout: float = 5.0) -> None: ...
    async def publish(event: Event) -> Event
    def subscribe(pattern: str, handler: SubscriberHandler, subscriber_id: str,
                  max_queue_depth: int | None = None, lifecycle_timeout: float | None = None) -> None
    def unsubscribe(subscriber_id: str) -> None
    async def replay(correlation_id: str, since_seq: int = 0) -> list[Event]
    def get_seq(session_id: str) -> int
    def clear_session(session_id: str) -> None
    def get_subscriber_queue(subscriber_id: str) -> BackpressureQueue | None
```

**Delivery semantics:** At-least-once, ordered per `session_id` by monotonic `seq` (assigned under an `asyncio.Lock`). Idempotency is enforced on `(session_id, seq)` pairs — a duplicate publish is a no-op. `causation_id`, when set, must reference an already-published event's `event_id` in the same session (validated on publish).

**Backpressure:** Each subscriber gets its own `BackpressureQueue` (bounded deque, default depth 1000). `TOKEN` events are coalescible and dropped oldest-first under pressure. Lifecycle events (matching `*.start`, `*.done`, `*.fail`, `task.done`, `build.done`, `question`, `error`) are never dropped — the producer blocks up to `lifecycle_timeout` (default 5s) and, on timeout, the event is persisted to spillover storage instead of being lost.

**Dependencies:** None (leaf component).

---

## Registry & Discovery

### `registry/` — Capability Registry

**Purpose:** Authoritative record of which capabilities are available and healthy right now. Per design R7, only Discovery (at boot) and the Health Monitor (continuously) write to it — everything else reads.

**Key class:** `CapabilityRegistry`

**Public API:**
```python
class CapabilityRegistry:
    def __init__(self, *, event_emitter: EventEmitter | None = None, session_id: str = "system") -> None: ...
    async def register(entry: CapabilityEntry) -> None
    async def deregister(capability: Capability) -> None
    def get(capability: Capability) -> CapabilityEntry            # raises CapabilityUnavailableError
    def has(capability: Capability) -> bool
    def has_kind(kind: CapabilityKind) -> bool
    def any_for_role(role: Role) -> bool
    def healthy_for_role(role: Role) -> list[CapabilityEntry]
    def summary() -> CapabilitySummary
    @property
    def entries() -> dict[Capability, CapabilityEntry]
```

Note: there is no `has_by_name()`/`has_by_kind()`/`get_all()`/`get_by_kind()` — use `has()`, `has_kind()`, and the `entries` property.

**Dependencies:** EventBus (via injected `event_emitter` callable).

**Events emitted:** `CAPABILITY_REGISTERED`, `CAPABILITY_DEREGISTERED`.

---

### `discovery/` — Discovery Bootstrap

**Purpose:** One-shot boot procedure: load/validate config YAML, probe all configured resources concurrently, register healthy ones, and report the outcome.

**Key function:** `run_discovery()` (plus helpers `load_and_validate_configs`, `extract_resources_from_config`, `probe_resources`).

**Public API:**
```python
async def run_discovery(
    config_dir: Path | str,
    registry: CapabilityRegistry,
    probe_map: dict[Capability, Probeable],
    event_emitter: Any | None = None,
    session_id: str = "boot",
) -> DiscoveryResult
```

Behavior: validates config (raises `ConfigValidationError` within ~1s on schema errors), probes each resource with a 5s per-probe timeout, registers healthy resources, emits `CAPABILITY_DEGRADED` for each unhealthy/timed-out resource, then emits `FORGE_BOOT_DISCOVERY_COMPLETE` with the healthy/unhealthy sets and duration.

**Dependencies:** CapabilityRegistry, config YAML files, `Probeable` objects (protocol with `health_check()`).

**Events emitted:** `CAPABILITY_DEGRADED`, `FORGE_BOOT_DISCOVERY_COMPLETE`.

---

### `health/` — Health Monitor

**Purpose:** The only continuous writer to the Registry after boot. Re-probes registered capabilities on an interval, deregisters after consecutive failures, re-registers after consecutive recoveries.

**Key classes:** `HealthMonitor`, `HealthMonitorConfig`.

**Public API:**
```python
class HealthMonitor:
    def __init__(self, *, registry: CapabilityRegistry, probe_map: dict[Capability, Any],
                 event_emitter: EventEmitter | None = None, audit_writer: AuditWriter | None = None,
                 config: HealthMonitorConfig | None = None, session_id: str = "system") -> None: ...
    async def start() -> None
    async def stop() -> None
    @property
    def running() -> bool
    def get_state(capability: Capability) -> _CapabilityHealthState | None
```

`HealthMonitorConfig` fields (all clamped): `probe_interval_s` (5–300, default 30), `probe_timeout_s` (1–60, default 10), `failure_threshold` (1–10, default 3), `recovery_threshold` (1–10, default 2).

**Dependencies:** CapabilityRegistry, `probe_map`, optional EventBus, optional AuditTrail (via `audit_writer`).

**Events emitted:** `CAPABILITY_DEGRADED`, `CAPABILITY_RECOVERED`. Also writes a `DecisionRecord` (kind `CAPABILITY_TRANSITION`) on every transition.

---

## AI Routing

### `router/` — Model Router

**Purpose:** Resolve a `Role` to a healthy provider+model via an ordered fallback chain, with per-provider circuit breakers, retries with cancellable exponential backoff, and session budget integration.

**Key classes:** `ModelRouter`, `RoleChainConfig`, `ChainEntry`, `ProviderCircuitBreaker`/`StubCircuitBreaker`, `CancellableBackoff`, `PermanentError`, `ModelUnavailableError`.

**Public API:**
```python
class ModelRouter:
    def __init__(self, *, chain_config: RoleChainConfig, registry_checker: RegistryChecker,
                 call_adapter: ModelCallAdapter, breakers: dict[str, CircuitBreaker] | None = None,
                 audit_recorder: AuditRecorder | None = None, cancel_event: asyncio.Event | None = None,
                 call_timeout: float = 60.0, max_retries: int = 3,
                 event_emitter: EventEmitter | None = None,
                 session_budget: SessionBudget | None = None, session_id: str = "") -> None: ...
    async def route(role: Role, messages: list[dict], **kwargs) -> str
    def cancel_backoff() -> None
    def reset_cancel() -> None
    @property
    def backoff() -> CancellableBackoff
```

Behavior: walks the chain in strict order, skipping providers not in the registry or with an open circuit breaker; per-call timeout is 60s (timeout counts as transient); permanent errors advance immediately to the next provider without retry; transient errors retry with jittered exponential backoff (1–30s) up to `max_retries`, cancellable via `cancel_event` (used by `interrupt/` for the 2s-cancel requirement). On exhaustion, raises `ModelUnavailableError` and records a `DecisionRecord` (kind `MODEL_FALLBACK`).

**Budget integration:** If constructed with a `session_budget` (a `SessionBudget` instance), `route()` calls `session_budget.check_budget(...)` before attempting the chain (pre-call estimation) and `session_budget.charge(...)` after a successful call (post-call charging). This is not optional wiring in production — it's how the router enforces per-session token limits.

**Dependencies:** CapabilityRegistry (via `registry_checker` callable), `call_adapter` (protocol), optional EventBus, optional SessionBudget, optional AuditTrail (via `audit_recorder`).

**Events emitted:** `MODEL_SELECTED`, `MODEL_FALLBACK`.

---

### `classifier/` — Intent Classifier & Router

**Purpose:** Deterministic, rules-based fast path for classifying user messages (interrupt/pause/resume/stop/status/build-intent/redirect) without invoking an AI model. Unmatched text falls back to AI classification via the Interrupt_Handler role.

**`classifier/__init__.py` — module functions, not a class:**
```python
def classify(
    message: str,
    build_state: BuildState,
    registry_checker: RegistryChecker | None = None,
) -> ClassificationResult
```
`IntentClass` enum: `INTERRUPT`, `PAUSE`, `RESUME`, `STOP`, `STATUS_QUERY`, `BUILD_INTENT`, `REDIRECT`, `NEEDS_AI_CLASSIFICATION`, `CLASSIFICATION_UNAVAILABLE`. `BuildState` is a frozen dataclass (`is_build_active`, `is_paused`) used to contextualize ambiguous commands (e.g. `interrupt` without an active build becomes `NEEDS_AI_CLASSIFICATION`). `ClassificationResult` carries `intent`, `confidence`, `matched_rule`.

**`classifier/router.py` — `IntentRouter` class (undocumented previously, real and worth knowing about):**
```python
class IntentRouter:
    def __init__(self, *, inspector_callback=None, async_inspector_callback=None,
                 ai_classifier_callback=None) -> None: ...
    def route(classification: ClassificationResult, *, message="", session_id="",
              build_state: BuildState | None = None) -> RoutingResult
    async def route_async(classification: ClassificationResult, *, message="", session_id="",
                           build_state: BuildState | None = None) -> RoutingResult
```
Maps each `IntentClass` to a `RoutingAction` (`STATUS_RESPONSE`, `BUILD_WORKFLOW`, `CONTROL_INTERRUPT`, `CONTROL_PAUSE`, `CONTROL_RESUME`, `CONTROL_STOP`, `CONTROL_REDIRECT`, `AI_CLASSIFICATION`, `UNAVAILABLE`, `NON_BUILD_MESSAGE`), returned as a `RoutingResult`. `status_query` is answered directly via the inspector callback — never via AI.

**Dependencies:** None for `classify()` (pure function). `IntentRouter` depends on caller-supplied callbacks (RuntimeInspector, ModelRouter/AI classifier).

**Events emitted:** None — classification and routing are pure/query operations.

---

## Session & Audit

### `session/` — Session Manager

**Purpose:** Synchronous CRUD for build sessions. No events.

**Key class:** `SessionManager` (fully synchronous — no `async def` anywhere in its public API).

**Public API:**
```python
class SessionManager:
    def __init__(self, secret_holder: SecretHolder | None = None) -> None: ...
    def create_session(*, repo_url: str, goal: str, build_mode: BuildMode | str = BuildMode.NEW,
                        vcs_token: str = "") -> Session
    def list_sessions() -> list[Session]
    def get_session(session_id: str) -> Session          # raises SessionNotFoundError
    def delete_session(session_id: str) -> None            # raises SessionNotFoundError
    def session_count() -> int
    def has_session(session_id: str) -> bool
```

There is no `update()` method — sessions are mutated in place via their fields where needed (e.g. `build_in_progress`), not through the manager. `create_session` stores the VCS token in the `SecretHolder` and keeps only a `TokenReference` on the `Session` object.

**Dependencies:** SecretHolder (constructor-injected).

**Events emitted:** None. Session lifecycle is not observable on the event bus — this is a pure state store.

---

### `audit/` — Audit Trail

**Purpose:** Persisted projection of the event stream plus the `DecisionRecord` store. Subscribes to the EventBus with pattern `"*"`.

**Key class:** `AuditTrail` (query and write methods are synchronous; only `handle_event` — the bus subscriber callback — is async).

**Public API:**
```python
class AuditTrail:
    async def handle_event(event: Event) -> None                 # EventBus subscriber handler
    def write_decision(*, kind: DecisionKind, subject: str, inputs: dict, decision: str,
                        alternatives: list[str] | None = None, caused_by_event_id: str | None = None,
                        session_id: str = "") -> DecisionRecord
    def get_events(session_id: str) -> list[Event]
    def get_decisions(session_id: str) -> list[DecisionRecord]
    def get_decision_by_subject(session_id: str, subject: str) -> DecisionRecord | None
    def get_retained_events() -> list[Event]
    def get_write_errors() -> list[AuditWriteError]
    def clear_session(session_id: str) -> None
    def subscribe_to_bus(bus: Any) -> None
```

There is no `get_last_decision()` — the closest equivalent is `get_decision_by_subject()`, which returns the most recent record for a given subject. Rationale strings are always deterministically template-built from `inputs` (never AI prose), so identical inputs always produce identical rationale text.

**Dependencies:** EventBus (subscribes via `subscribe_to_bus`).

**Events emitted:** None — it's a subscriber/projection, not a publisher.

---

### `secrets/` — Secret Holder

**Purpose:** Per-session in-memory-only secret storage with redaction at every serialization boundary. Never persisted to disk, DB, audit trail, or logs.

**Key class:** `SecretHolder` (a `@dataclass`, fully synchronous).

**Public API:**
```python
@dataclass
class SecretHolder:
    def store_secret(session_id: str, key_name: str, value: str) -> None
    def get_secret(session_id: str, key_name: str) -> str | None
    def has_secret(session_id: str, key_name: str) -> bool
    def list_keys(session_id: str) -> list[str]
    def get_token_reference(session_id: str, key_name: str = "vcs_token") -> TokenReference
    def redact(data: Any, *, session_id: str | None = None) -> Any
    def redact_or_raise(data: Any, *, session_id: str | None = None) -> Any   # raises RedactionError
    def clear_session(session_id: str) -> None
    def clear_all() -> None
    def session_count() -> int
    def has_session(session_id: str) -> bool
```

`TokenReference` is a frozen dataclass (`session_id`, `key_name`, `placeholder`) — a non-reversible stand-in stored in session state instead of the raw secret.

**Dependencies:** None.

**Events emitted:** None.

---

### `inspector/` — Runtime Inspector

**Purpose:** Read-only query facade answering "what is happening?" and "why?" — derives everything from `ForgeState`, `AuditTrail`, and `CapabilityRegistry`. Never invokes an AI model. All public methods are synchronous.

**Key class:** `RuntimeInspector`.

**Public API:**
```python
class RuntimeInspector:
    def __init__(self, *, audit_trail: AuditTrail, registry: CapabilityRegistry,
                 state_provider: _StateProvider | None = None) -> None: ...
    def get_status(session_id: str) -> RuntimeStatus
    def current_node(session_id: str) -> str
    def worker_status(session_id: str) -> WorkerStatus
    def task_queue(session_id: str) -> list[TaskView]
    def active_task(session_id: str) -> TaskView | None
    def explain_last_decision(session_id: str) -> DecisionRecord     # raises DecisionNotFoundError
    def explain_task(session_id: str, task_id: str) -> TaskExplanation  # raises TaskNotFoundError
    def capability_summary() -> dict[str, Any]
```

Note: the method is `capability_summary()`, not `get_capabilities()`. `explain_task()` reconstructs a task's history (model selections, verifier outcomes, retries, resulting commit, policy decisions) by scanning `AuditTrail` events/decisions filtered by `task_id` — this is the causation-link reconstruction referenced in the requirements.

**Dependencies:** AuditTrail, CapabilityRegistry, an internal `_StateProvider` (in-memory `ForgeState`/`SessionBudget` lookup, swappable).

**Events emitted:** None — read-only by design.

---

## Workflow Logic

### `clarification/` — Clarification Workflow

**Purpose:** Identify missing specification inputs, emit clarification questions, record developer answers into `SessionContext`, and advance once satisfied. Implemented as module-level functions plus a `SessionContext` dataclass — there is no `ClarificationEngine` class.

**Key types:** `SessionContext` (dataclass with `goals`, `decisions`, `assumptions`, `constraints`, `preferences` — fixed field order for deterministic `serialize_to_prompt()`), `SpecificationInput`.

**Public API:**
```python
def get_missing_inputs(context: SessionContext, required_inputs=None) -> list[SpecificationInput]
async def emit_question(input_spec: SpecificationInput, event_emitter: EventEmitter, *,
                         session_id: str = "", causation_id: str | None = None) -> Event
def record_answer(context: SessionContext, input_name: str, answer: str, *,
                   required_inputs=None) -> bool
def advance_if_ready(context: SessionContext, required_inputs=None) -> bool
async def run_clarification(context: SessionContext, event_emitter: EventEmitter, *,
                             session_id: str = "", max_questions: int = 5,
                             required_inputs=None, causation_id=None) -> list[Event]
async def handle_answer(context: SessionContext, input_name: str, answer: str,
                         event_emitter: EventEmitter, *, session_id: str = "",
                         max_questions: int = 5, required_inputs=None,
                         causation_id=None) -> tuple[bool, Event | None]
```
Empty/whitespace-only answers are rejected by `record_answer` (returns `False`) and `handle_answer` re-emits the same question rather than advancing.

**Dependencies:** An `event_emitter` callable (typically `EventBus.publish`).

**Events emitted:** `QUESTION` (one per missing input, up to `max_questions`).

---

### `specification/` — Specification Generator

**Purpose:** Invoke the Architect role exactly once, parse its response into a specification + task list, save the artifact, and emit readiness events.

**Key class:** `SpecificationGenerator`.

**Public API:**
```python
class SpecificationGenerator:
    def __init__(self, *, model_invoke: ModelInvokeFunc, artifact_store: ArtifactSaveFunc,
                 event_emitter: EventEmitter) -> None: ...
    async def generate(session_context: dict, digital_twin: dict, *,
                        session_id: str = "") -> SpecificationArtifact
```
Module-level helpers: `store_specification`, `get_specification` (raises `SpecificationNotFoundError`), `clear_specification`, `clear_all_specifications` — backed by an in-memory per-session store (most-recent-wins).

**Dependencies:** ModelRouter (via `model_invoke` callable, invoked for `Role.ARCHITECT`), an artifact store callable, EventBus.

**Events emitted:** `SPEC_READY` (uri, version), `TASKS_READY` (task_count, graph_ref), `ERROR` (on generation or save failure — `SpecificationGenerationError`/`ArtifactSaveError`).

---

### `planner/` — Dependency Planner

**Purpose:** Build a dependency DAG from a task list, validate it, detect cycles, and produce a topological ordering. Implemented as a single pure function — there is no `TaskPlanner` class and it emits no events.

**Public API:**
```python
def plan(tasks: list[dict[str, Any]]) -> PlanResult
```
`PlanResult` has `ordering: list[str]` and `errors: list[PlanError]`, with `is_ok` true iff there are no errors. `PlanError.kind` is `PlanErrorKind.CYCLE_DETECTED` or `PlanErrorKind.UNRESOLVED_DEPENDENCY`. Unresolved dependencies are checked before cycle detection; cycles are detected via DFS coloring; the final ordering uses Kahn's algorithm with sorted tie-breaking for determinism.

**Dependencies:** None (pure function over plain dicts).

**Events emitted:** None.

---

### `dispatcher/` — Task Dispatcher

**Purpose:** Execute a topologically-ordered task list sequentially (parallelism = 1), requesting an isolated workspace per task and confining all writes to it — the canonical repo is never touched during execution.

**Key class:** `TaskDispatcher`.

**Public API:**
```python
class TaskDispatcher:
    def __init__(self, workspace_provider: WorkspaceProvider, event_publisher: EventPublisher,
                 canonical_path: str, session_id: str, base_ref: str = "HEAD") -> None: ...
    def load_ordering(tasks: list[dict], ordering: list[str]) -> None
    async def dispatch_all(executor: TaskExecutor) -> DispatchResult
    def is_write_allowed(path: str, task_id: str | None = None) -> bool
    @property
    def pending_task_ids() -> list[str]
    @property
    def completed_task_ids() -> list[str]
    @property
    def failed_task_ids() -> list[str]
    @property
    def skipped_task_ids() -> list[str]
```
There is no `dispatch(task, workspace_id)` or `next_task(state)` — the whole ordering is loaded via `load_ordering()` and executed in one call to `dispatch_all(executor)`, which internally creates/destroys a workspace per task and skips tasks whose dependencies failed or were skipped.

**Dependencies:** `WorkspaceProvider` (protocol — typically `WorkspaceManager`), `EventPublisher` (typically `EventBus`).

**Events emitted:** `TASK_START`, `TASK_DONE`, `TASK_FAIL`.

---

### `policies/` — Policy Engine

**Purpose:** YAML-driven, deterministic retry/escalate/skip decisions on verification or tool failure, bounded by a retry budget.

**Key classes:** `PolicyEngine`, `PolicyConfig`, `PolicyRule`, `PolicyResult`.

**Public API:**
```python
class PolicyEngine:
    def __init__(self, *, config: PolicyConfig, event_emitter: EventEmitter | None = None,
                 registry_checker: RegistryChecker | None = None, session_id: str = "") -> None: ...
    async def decide(*, task_id: str, stage_name: str, attempt_count: int,
                      tool_failures: int = 0) -> PolicyResult
    def is_task_failed(task_id: str) -> bool
    def can_execute(task_id: str, depends_on: list[str] | None = None) -> bool
    @property
    def decisions() -> list[DecisionRecord]
```
`decide()` is async (not sync) and takes keyword-only arguments. Priority order: retry-budget exhaustion → tool-failure escalation (checked against the registry for an alternate coding tool) → stage-specific rule match → default skip. Every call emits exactly one `POLICY_DECISION` event and writes exactly one `DecisionRecord`; a `FAILED` decision additionally emits `TASK_FAIL`.

**Dependencies:** `PolicyConfig` (parsed from `policies.yaml` via `parse_policy_config`), optional EventBus, optional CapabilityRegistry (via `registry_checker`).

**Events emitted:** `POLICY_DECISION`, `TASK_FAIL` (on failed decisions).

---

### `mode/` — Mode Evaluator

**Purpose:** Determine OPERATIONAL vs DEGRADED mode from the Capability Registry and emit the mode transition plus `FORGE_READY`.

**Key class:** `ModeEvaluator`.

**Public API:**
```python
class ModeEvaluator:
    def __init__(self, registry: CapabilityRegistry, *, event_emitter: EventEmitter | None = None,
                 session_id: str = "system") -> None: ...
    def evaluate() -> ModeEvaluationResult
    async def evaluate_and_emit() -> ModeEvaluationResult
    @property
    def current_mode() -> OperationalMode | None
```
The result type is `ModeEvaluationResult` (not `ModeResult`), and `current_mode` is a read-only property (not a method call). OPERATIONAL requires a Coder-role provider, a VCS connector, and a coding tool; missing any one forces DEGRADED. Soft degradations (missing Reviewer role, missing vector store) are recorded as `DecisionRecord`s but don't affect the mode.

**Dependencies:** CapabilityRegistry, optional EventBus.

**Events emitted:** `CAPABILITY_DEGRADED`/`CAPABILITY_RECOVERED` (on a mode transition), `FORGE_READY` (every `evaluate_and_emit()` call, carrying mode, `CapabilitySummary`, and a startup report).

---

## Execution

### `workspace/` — Workspace Manager

**Purpose:** Create, track, and destroy isolated sandbox directories per task.

**Key classes:** `WorkspaceManager`, `WorkspaceInfo` (not a `Workspace` type).

**Public API:**
```python
class WorkspaceManager:
    def __init__(self, event_bus: EventBus, base_dir: str | None = None,
                 max_workspace_age: int = 3600) -> None: ...
    async def create(task_id: str, session_id: str, base_ref: str = "main",
                      max_concurrent: int = 10) -> WorkspaceInfo
    async def destroy(workspace_id: str, reason: str = "task_complete") -> bool
    def get_workspace(workspace_id: str) -> WorkspaceInfo | None
    def list_workspaces(session_id: str | None = None) -> list[WorkspaceInfo]
    async def reap_orphans(active_task_ids: set[str] | None = None) -> list[str]
    async def destroy_session_workspaces(session_id: str) -> list[str]
```
Note the parameter order for `create()`: `(task_id, session_id, base_ref, max_concurrent)` — not `(session_id, task_id)`. The lookup method is `get_workspace()`, not `get()`. Creation enforces a hard ceiling on concurrent active workspaces (`WorkspaceLimitExceededError` if exceeded).

**Dependencies:** EventBus, filesystem (`tempfile`/`shutil`).

**Events emitted:** `WORKSPACE_CREATED`, `WORKSPACE_DESTROYED`, `ERROR` (on creation failure).

---

### `commit/` — Commit Workflow

**Purpose:** Commit a verified task's changes into the canonical repository, enforcing an approval gate when required. Pushing to the remote is a separate concern (see `finalization/`) — there is no `push()` here.

**Key class:** `CommitWorkflow` (not `CommitHandler`).

**Public API:**
```python
class CommitWorkflow:
    def __init__(self, *, vcs_committer: VCSCommitter, event_emitter: EventEmitter | None = None,
                 approval_provider: ApprovalProvider | None = None, session_id: str = "",
                 canonical_repo_path: str = "") -> None: ...
    def mark_verified(task_id: str) -> None
    def is_verified(task_id: str) -> bool
    async def commit_task(request: CommitRequest) -> CommitResult    # raises CommitNotVerifiedError
```
`commit_task()` raises `CommitNotVerifiedError` if `verify.passed` hasn't been recorded for the task via `mark_verified()`. If `CommitRequest.approval_required` is set, it requests approval before committing and returns a `REJECTED` result (never raises) if approval is denied.

**Dependencies:** `VCSCommitter` (protocol), optional EventBus, optional `ApprovalProvider`.

**Events emitted:** `COMMIT_DONE`, `COMMIT_FAILED`, `APPROVAL_PENDING`, `APPROVAL_REJECTED`.

---

### `documentation/` — Documentation Maintenance

**Purpose:** Keep documentation in sync with code by diffing the Digital Twin on commit, updating docs via a `DocWriter`, tracking drift when updates fail or are skipped, and supporting a full-sync `document` build mode.

**Key class:** `DocumentationMaintenance` (not `DocumentationManager`).

**Public API:**
```python
class DocumentationMaintenance:
    def __init__(self, *, doc_writer=None, twin_state=None, event_emitter=None,
                 session_id: str = "", workspace_path: str = "") -> None: ...
    def compute_twin_diff(commit_sha: str, changed_files: list[str]) -> TwinDiff
    async def on_commit_done(commit_sha: str, changed_files: list[str]) -> CommitDoneResult
    async def on_finalization() -> list[DocDriftEntry]
    async def run_document_mode() -> list[DocUpdateResult]
```
`CommitDoneResult` supports both tuple-unpacking (`diff, drift = await maintenance.on_commit_done(...)`) and attribute access. There's also a module-level `compute_twin_diff_from_twins()` helper for diffing raw file-index sets outside an instance.

**Dependencies:** `DocWriterProtocol` (optional — drift is recorded when absent), `TwinStateProvider` (optional), optional EventBus.

**Events emitted:** `DOC_UPDATED` (successful doc writes), `DOC_DRIFT` (failed/skipped updates, or modules changed without corresponding doc updates at finalization).

---

## Verification

### `verification/` — Verification Pipeline

**Purpose:** Run advisory stages concurrently, then blocking stages sequentially in declared order; halt on first blocking failure.

**Key class:** `VerificationPipeline`.

**Public API:**
```python
class VerificationPipeline:
    def __init__(self, *, advisory_stages: list[VerificationStage] | None = None,
                 blocking_stages: list[VerificationStage] | None = None,
                 event_emitter: EventEmitter | None = None,
                 session_id: str = "", task_id: str = "") -> None: ...
    async def run() -> PipelineResult
```
`run()` takes no arguments — the stages are fixed at construction time via `advisory_stages`/`blocking_stages` (lists of `VerificationStage(name, timeout_s, executor)`). There is no `add_stage()` method; to change stages, construct a new pipeline. `PipelineResult` carries `advisory_results`, `blocking_results`, `all_blocking_passed`, `halted_at`, and (on failure) a `policy_request` payload intended for the `PolicyEngine`.

**Dependencies:** Caller-supplied stage executors (async callables returning `(passed: bool, detail: str)`), optional EventBus.

**Events emitted:** `VERIFY_STAGE` (per stage, advisory and blocking), `VERIFY_PASSED` (when all blocking stages pass).

---

## Observability

### `budget/` — Session Budget

**Purpose:** Per-session token/cost governance enforced by the Model Router before and after each call.

**Key class:** `SessionBudget`.

**Public API:**
```python
class SessionBudget:
    def __init__(self, token_limit: int, session_id: str = "",
                 event_emitter: EventEmitter | None = None) -> None: ...
    async def check_budget(estimated_tokens: int, role: str) -> None   # raises BudgetExceededError
    def charge(actual_tokens: int) -> None
    def summary() -> dict[str, Any]
    @property
    def limit() -> int
    @property
    def consumed() -> int
    @property
    def remaining() -> int
```
Real names: `charge()` (not `consume()`), async `check_budget()` (not sync `check()`), `remaining` is a property (not a method), `summary()` (not `usage_report()`). `check_budget()` emits `BUDGET_EXCEEDED` and raises before the call is attempted; `charge()` is called after a successful call completes.

**Dependencies:** Optional EventBus.

**Events emitted:** `BUDGET_EXCEEDED`.

---

### `learning/` — Learning Recorder

**Purpose:** Record one outcome entry per executed task at build finalize, and surface (never auto-apply) recommendations derived from recorded outcomes.

**Key class:** `LearningRecorder`.

**Public API:**
```python
class LearningRecorder:
    def __init__(self, event_emitter: EventEmitter | None = None) -> None: ...
    def record_outcome(entry: OutcomeEntry) -> None                       # sync
    async def record_build_outcomes(session_id: str,
                                     task_results: list[dict[str, Any]]) -> None  # async batch
    def get_outcomes(session_id: str) -> list[OutcomeEntry]
    def get_recommendations(session_id: str | None = None) -> list[Recommendation]
```
`record_outcome()` is a single, synchronous persist call; `record_build_outcomes()` is the async batch entry point used at finalize — it builds an `OutcomeEntry` per task result and calls `record_outcome()` for each, emitting an `ERROR` event (without discarding the build result) if an individual entry fails to construct. `get_recommendations()` is heuristic-based (tool/model failure-rate, high retry counts) and is purely advisory.

**Dependencies:** Optional `EventEmitter` protocol (object with `async publish(event)`).

**Events emitted:** `ERROR` (on individual outcome recording failure only).

---

### `recovery/` — Crash Recovery

**Purpose:** Checkpoint workflow state after each node, resume interrupted sessions on restart, and replay missed events to reconnecting clients.

**Key class:** `CrashRecovery`.

**Public API:**
```python
class CrashRecovery:
    def __init__(self, *, checkpoint_store: CheckpointStore, event_bus: EventPublisher | None = None,
                 event_replayer: EventReplayer | None = None,
                 workspace_destroyer: WorkspaceDestroyer | None = None,
                 task_requeuer: TaskRequeuer | None = None, redact_state: Any | None = None,
                 retained_event_window: int = 1000) -> None: ...
    async def checkpoint_after_node(session_id: str, node_id: str, state: dict,
                                     highest_seq: int) -> None     # raises CheckpointWriteError
    async def resume_all() -> list[ResumeResult]
    async def handle_client_reconnect(session_id: str,
                                       last_received_seq: int) -> ClientReconnectResult
    def record_commit(session_id: str, task_id: str) -> None
    def register_task_workspace(task_id: str, workspace_id: str) -> None
    def update_oldest_retained_seq(session_id: str, oldest_seq: int) -> None
    def clear_session(session_id: str) -> None
```
There is no `recover()`/`list_recoverable()` — the real names are `resume_all()` (finds and resumes every session whose latest checkpoint is non-terminal) and `handle_client_reconnect()` (replays events since the client's last seen `seq`, or sends a full state snapshot + retained events if the client's last seq predates the retained window). `checkpoint_after_node()` raises `CheckpointWriteError` and emits an `ERROR` event on persistence failure — the caller must halt, not advance.

**Dependencies:** `CheckpointStore` (protocol; `InMemoryCheckpointStore` provided for tests), optional EventBus, optional `EventReplayer` (typically `EventBus.replay`), optional `WorkspaceDestroyer`, optional `TaskRequeuer`, optional `redact_state` callable (typically backed by `SecretHolder`).

**Events emitted:** `ERROR` (checkpoint write failure), `WORKSPACE_DESTROYED` (cleaning up uncommitted tasks' workspaces during resume).

---

### `interrupt/` — Interrupt Handler

**Purpose:** Pause, resume, redirect, and stop an active build session, each completing within 2 seconds. Cancels any in-flight `ModelRouter` backoff immediately on pause/stop.

**Key class:** `InterruptHandler`. There is no unified `interrupt()` dispatcher — pause/resume/redirect/stop are four separate methods.

**Public API:**
```python
class InterruptHandler:
    def __init__(self, *, cancel_backoff: CancelBackoffCallback | None = None,
                 event_emitter: EventEmitter | None = None,
                 planning_callback: PlanningCallback | None = None,
                 time_func: Callable[[], float] | None = None) -> None: ...
    async def pause(session_id: str, message: str, build_state: dict | None = None) -> PausedState
    async def resume(session_id: str) -> dict[str, Any]        # raises NotPausedError
    async def redirect(session_id: str, direction: str) -> dict[str, Any]  # raises EmptyDirectionError, NotPausedError
    async def stop(session_id: str) -> None
    def is_paused(session_id: str) -> bool
    def get_interrupt_message(session_id: str) -> str
    def get_retained_state(session_id: str) -> dict[str, Any]
```
`is_paused()`, not `is_interrupted()`. `resume()` on a session that isn't paused raises `NotPausedError`; `redirect()` with an empty/whitespace direction raises `EmptyDirectionError` without clearing the paused state.

**Dependencies:** A `cancel_backoff` callable (typically `ModelRouter.cancel_backoff`), optional EventBus, optional `planning_callback` (re-enters planning with the new direction on `redirect()`).

**Events emitted:** `INTERRUPT_PAUSED`, `INTERRUPT_RESUMED`, `INTERRUPT_STOPPED`, `INTERRUPT_REDIRECTED`.

---

### `finalization/` — Finalization Workflow

**Purpose:** Push the canonical repository to the remote VCS after all tasks are processed, and emit the build summary. This is where `push()` actually lives (not in `commit/`).

**Key class:** `FinalizationWorkflow`.

**Public API:**
```python
class FinalizationWorkflow:
    def __init__(self, event_publisher: EventPublisher, vcs_pusher: VCSPusher,
                 audit_writer: AuditWriter, session_id: str, canonical_path: str,
                 approval_timeout: float = 300, push_requires_approval: bool = False) -> None: ...
    async def finalize(committed_tasks: list[str], skipped_tasks: list[str],
                        failed_tasks: list[str], total_tasks: int | None = None) -> BuildSummary | None
    def respond_to_approval(request_id: str, approved: bool) -> bool
    @property
    def pending_approvals() -> dict[str, ApprovalRequest]
```
`finalize()` returns `None` (not an exception) both when push is approval-gated and rejected/times out, and when the actual VCS push fails — in the failure case commits are retained and an `ERROR` event is emitted instead of `BUILD_DONE`.

**Dependencies:** `VCSPusher` (protocol), `EventPublisher`, `AuditWriter` (typically `AuditTrail.write_decision`).

**Events emitted:** `BUILD_DONE` (success), `ERROR` (push failure or approval not granted), `QUESTION` (approval pending).

---

### `boundaries/` — Layer Boundary Enforcement

**There are two independent, non-identical implementations of this check in the codebase.** They are not shared code and their layer definitions disagree — this is a real inconsistency, not a documentation error.

**1. `backend/app/runtime/boundaries/__init__.py`** — functions `check_boundaries()` / `enforce_boundaries()`.
- `Layer` is an `IntEnum`: `PRESENTATION=1, APPLICATION=2, RUNTIME=3, ADAPTER=4, INFRASTRUCTURE=5`.
- Adjacency is strictly "self or one layer below" (`PRESENTATION→APPLICATION`, `APPLICATION→RUNTIME`, `RUNTIME→ADAPTER`, `ADAPTER→INFRASTRUCTURE`).
- Also does content-level checks: Runtime must not import transport modules (`fastapi`, `starlette`, `uvicorn`, `flask`, `django`, etc.), Application must not import a hardcoded list of "engineering" runtime submodules, Adapter must not import a hardcoded list of "business logic" runtime submodules.
```python
def check_boundaries(root_path: str | None = None) -> list[BoundaryViolation]
def enforce_boundaries(root_path: str | None = None) -> None   # raises BoundaryCheckError
```

**2. `backend/app/boundaries.py`** — functions `check_all_boundaries()` / `check_module_boundaries()`.
- `Layer` is a `str` Enum: `PRESENTATION, APPLICATION, RUNTIME, ADAPTER, SHARED` — note `SHARED` here where the other module has `INFRASTRUCTURE`, and there is no `INFRASTRUCTURE` layer at all in this version.
- Adjacency also lets `RUNTIME` and `ADAPTER` both import `SHARED` directly (not present in the other implementation).
- Forbidden-imports table only currently populates a restriction for `RUNTIME` (no `fastapi`/`starlette`/etc.); `APPLICATION` and `ADAPTER` forbidden sets are empty placeholders.
```python
def check_all_boundaries(app_root: str | Path) -> list[BoundaryViolation]
def check_module_boundaries(module_path: str, source: str) -> list[BoundaryViolation]
```

**Recommendation:** these should be reconciled into a single implementation — the differing layer enums (`INFRASTRUCTURE` vs `SHARED`) and differing adjacency/forbidden-import rules mean the two checks can disagree about whether the same import is a violation. Until reconciled, do not assume either one is "the" boundary checker.

**Dependencies:** None (static AST analysis of source files).

**Events emitted:** None.

---

## Configuration

### `config/` — ConfigService

**Purpose:** Single source of truth for Forge's runtime configuration (API keys, selected model, sandbox mode). Loads from and atomically persists to a JSON file, applies changes to the live runtime without a restart, and exposes health/key-testing probes used by the setup wizard.

**Key classes:** `ConfigService`, `ConfigState`, `SandboxMode`, `KeyTestResult`.

**Public API:**
```python
class ConfigService:
    def __init__(self, config_path: Path, event_emitter: EventEmitter) -> None: ...
    def set_deps(deps: RuntimeDeps) -> None
    def apply_to_runtime() -> None
    async def load() -> ConfigState
    async def get_config() -> dict[str, Any]                 # secrets redacted
    async def update_config(payload: dict[str, Any]) -> dict[str, Any]  # raises ConfigValidationError
    async def test_key(component: str, key: str | None = None) -> KeyTestResult
    async def get_component_health() -> dict[str, dict[str, Any]]
    async def get_models() -> list[dict[str, str]]
    @property
    def state() -> ConfigState
```
`ConfigState` fields: `openrouter_api_key`, `github_token`, `selected_model`, `sandbox_mode` (`SandboxMode`: `ALWAYS`/`AUTO`/`NEVER`), `model_cache_ttl_seconds` (default 3600), and a derived `configured` property (true once an API key and model are set).

`apply_to_runtime()` hot-reloads three things when config changes, without restarting the process: the Model Router's chain config (points every role at the newly selected model), the `FORGE_USE_SANDBOX` env var (read by the coding tool per-execution), and the OpenRouter call adapter (recreated only if the API key actually changed). This requires `set_deps()` to have been called with the `RuntimeDeps` container during bootstrap — without it, `apply_to_runtime()` is a no-op.

Persistence is atomic (write to `.tmp`, `os.chmod` to `0600` where supported, then `os.replace`). Secret fields (`openrouter_api_key`, `github_token`) are redacted in `get_config()` output, preserving known key prefixes and the last 4 characters for UX (e.g. `sk-****xyz1`).

**Dependencies:** EventBus (via `event_emitter`), filesystem, `RuntimeDeps` (optional, for hot-reload), `httpx` (for `test_key`/`get_component_health`/`get_models` probes against OpenRouter/GitHub).

**Events emitted:** `CONFIG_ERROR` (on corrupt config file at load time).

---

## Shared (`app/runtime/models.py`, `app/runtime/types.py`)

Domain types shared across runtime modules, defined once in `backend/app/runtime/models.py`:

| Type | Description |
|------|--------------|
| `BuildMode` | Enum: kind of build requested for a session (new/extend/analyze/document). |
| `OperationalMode` | Enum: `OPERATIONAL` / `DEGRADED`. |
| `Capability` | Enum of named, discoverable capabilities (e.g. `AI_CODER`, `VCS_GITHUB`, `TOOL_AIDER`). |
| `CapabilityKind` | Enum of capability categories for kind-based lookups (`AI_PROVIDER`, `VCS_CONNECTOR`, `CODING_TOOL`, `VECTOR_STORE`, ...). |
| `Role` | Enum of abstract AI roles the Model Router resolves (`ARCHITECT`, `PLANNER`, `CODER`, `REVIEWER`, `DOC_WRITER`, `INTERRUPT_HANDLER`, `CLARIFICATION`). |
| `CapabilityEntry` | Frozen dataclass: a single registered capability (name, kind, healthy, roles, provider_name, metadata). |
| `CapabilitySummary` | Frozen dataclass snapshot of available/degraded/missing-required capabilities and operability. |
| `Task` | A single unit of work in a build plan. |
| `ForgeState` | `TypedDict` — the LangGraph workflow state object threaded through the build graph. |
| `SessionContext` | Persistent per-session working memory (also re-declared locally in `clarification/__init__.py` with the same shape). |
| `DocFileEntry`, `DocumentationState` | First-class model of documentation tracked by the Digital Twin. |
| `FileEntry`, `DigitalTwin` | Lean structured model of the repository. |

`app/runtime/types.py` re-exports select shared types (e.g. `Health`, `PermanentError`) for backward-compatible imports across adapters and runtime modules.
