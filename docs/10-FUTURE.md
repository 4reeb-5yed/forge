# Future — Roadmap & Limitations

## What Works Today

| Capability | Status |
|-----------|--------|
| Full runtime with 27 modules | ✅ Working, tested |
| LangGraph state machine (13 nodes, 4 routing functions) | ✅ Compiled, wired |
| Bootstrap sequence (discovery → registry → health → mode → ready) | ✅ Functional |
| Event Bus with pub/sub, ordering, wildcards | ✅ Production-ready |
| Capability Registry with health-based ejection | ✅ Production-ready |
| Model Router with fallback chains + circuit breakers | ✅ Production-ready |
| Policy Engine (retry/skip/escalate decisions) | ✅ Production-ready |
| Session Budget enforcement | ✅ Production-ready |
| Interrupt handling (pause/resume/stop < 2s) | ✅ Production-ready |
| Secret redaction at all serialization boundaries | ✅ Production-ready |
| Crash recovery from checkpoints | ✅ Production-ready |
| OpenRouter adapter (complete + stream + error classification) | ✅ Implemented |
| GitHub VCS adapter (clone/commit/push + token sanitization) | ✅ Implemented |
| Aider tool adapter (subprocess + timeout) | ✅ Implemented |
| PostgreSQL schema + stores + migrations | ✅ Ready to use |
| Docker Compose deployment | ✅ Working |
| Next.js frontend with real-time events | ✅ Working |
| 1,240+ tests (unit + property-based) | ✅ All passing |
| API authentication (Bearer token) | ✅ Working |
| WebSocket event streaming | ✅ Working |
| Boundary enforcement (architectural drift prevention) | ✅ Working |

## Known Limitations

### 1. In-Memory Stores Still Used in Workflow

The `assemble_deps()` function in `bootstrap.py` creates in-memory implementations for development. While PostgreSQL stores are implemented and tested, the swap to persistent stores requires wiring at the dependency assembly level.

**Impact:** Session data, audit logs, and checkpoints are lost on restart unless the PostgreSQL stores are explicitly wired.

### 2. No Real AI Calls Without API Keys

Without `OPENROUTER_API_KEY`, the system starts in DEGRADED mode. The `clarify`, `architect`, and `plan` nodes use a no-op call adapter that returns `{"result": "no-op"}`.

**Impact:** End-to-end builds require real API keys. The runtime logic is fully exercisable without them (via tests), but you can't see a real build complete.

### 3. Sequential Task Execution

Tasks are executed one at a time. The architecture has a **parallelism seam** (task_ordering is a DAG, not a list), but the current implementation processes tasks serially via `current_task_index`.

**Impact:** Builds with many independent tasks take longer than necessary.

### 4. Single-Process Architecture

Everything runs in one asyncio event loop. There's no task queue, no worker pool, no distributed execution.

**Impact:** Limited to one concurrent build at a time. Multiple sessions can exist, but only one is actively executing.

### 5. No Approval Gates UI

The `approval_pending` flag exists in ForgeState but there's no frontend UI to present approval requests and capture user decisions mid-build.

**Impact:** Builds either run fully autonomously or get stuck waiting for approval that the UI can't surface.

### 6. Learning Engine is Record-Only

The `LearningRecorder` stores outcomes but doesn't yet use them to influence future builds. There's no recommendation engine reading from the learning store.

**Impact:** The system records what works and what doesn't, but doesn't learn from it yet.

### 7. No Multi-Repo Support

Each session targets a single repository. There's no support for builds that span multiple repos.

### 8. Workspace Cleanup

Workspaces are created per-task but cleanup is basic. There's no automated TTL-based garbage collection.

## Planned Improvements

### Short-Term (Next Sprint)

| Improvement | Description |
|------------|-------------|
| Wire PostgreSQL stores | Replace in-memory stores in `assemble_deps()` with real DB stores |
| Frontend approval UI | Surface `approval_pending` state with approve/reject buttons |
| Workspace TTL cleanup | Background task that cleans up completed workspaces after N hours |

### Medium-Term (1–3 Months)

| Improvement | Description |
|------------|-------------|
| Parallel task execution | Execute independent tasks concurrently using the DAG ordering |
| Real-time streaming to frontend | Stream AI token output to the chat UI as it arrives |
| Multi-model evaluation | Try multiple models for architect/plan and pick the best result |
| Retry budgets per provider | Track failures per provider per session, not just globally |
| OpenHands adapter | Add [OpenHands](https://github.com/All-Hands-AI/OpenHands) as an alternative coding tool |

### Long-Term (3–6 Months)

| Improvement | Description |
|------------|-------------|
| Multi-repo support | Builds that coordinate changes across multiple repositories |
| Learning engine recommendations | Use recorded outcomes to suggest tools, models, and strategies |
| Worker architecture | Separate API tier from build workers for horizontal scaling |
| Prometheus metrics | Export runtime metrics for monitoring |
| Plugin system | Third-party adapters installable as Python packages |
| Approval gates UI | Full approval workflow with escalation to team leads |

## Pros of Current Design

### 1. Testability

Every module depends on protocols, not implementations. The entire runtime is testable without any external services, yielding 1,240+ fast tests.

### 2. Observability

The Event Bus design means every operation is recorded. You can reconstruct exactly what happened in any build from the audit log alone.

### 3. Extensibility

Adding a new adapter, a new workflow node, or a new runtime module follows a documented pattern. The boundary checker prevents accidental coupling.

### 4. Correctness Confidence

Property-based testing with Hypothesis gives confidence in invariants across thousands of random inputs — not just the happy paths a developer thought to test.

### 5. Graceful Degradation

The system never crashes on missing capabilities. It evaluates what's available, reports what's missing, and refuses work it can't complete safely.

### 6. Security by Design

Secrets are in-memory only, redacted at serialization boundaries, and never logged. Token injection is localized to a single adapter method.

## Cons / Tradeoffs Made

### 1. Sequential-First Execution

Chose simplicity over performance. The parallelism seam exists but isn't active, meaning builds are slower than they could be.

**Rationale:** Correctness first. Parallel execution introduces complex failure modes (partial commits, resource contention) that we want to handle deliberately.

### 2. In-Memory as Default

The runtime boots with in-memory stores by default, requiring explicit wiring for persistence.

**Rationale:** Zero external dependencies for development and testing. A developer can run the full test suite without Docker or PostgreSQL.

### 3. Single-Process Simplicity

No distributed architecture, no message queue, no worker pool.

**Rationale:** Reduced operational complexity. A single process is easier to deploy, debug, and reason about. Scaling is a future concern for when demand justifies the complexity.

### 4. No AI During Classification

Intent classification is rule-based (keyword matching), not AI-powered.

**Rationale:** Determinism. "stop" must always mean stop, regardless of whether AI providers are available. The classifier must work in DEGRADED mode.

### 5. LangGraph Dependency

The workflow is tightly coupled to LangGraph's StateGraph abstraction.

**Rationale:** LangGraph provides checkpointing, state management, and routing out of the box. The alternative (hand-rolled state machine) would duplicate significant work for marginal flexibility gains.

### 6. Heavy Module Count

27 runtime modules is a lot. Each one is small, but the surface area is large.

**Rationale:** Each module has one responsibility. This makes testing trivial, boundaries clear, and allows any module to be replaced without touching others. The tradeoff is discovery cost for new developers (hence this documentation).
