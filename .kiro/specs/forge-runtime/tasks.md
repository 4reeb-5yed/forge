# Implementation Plan: Forge Runtime

## Overview

This plan implements the Forge autonomous software engineering runtime — a Python asyncio/FastAPI/LangGraph system that orchestrates builds across isolated workspaces with typed events, health-aware model routing, staged verification, and explainable audit trails. The implementation builds incrementally on the existing foundation (models, protocols, event types, types) and targets all 26 requirements and 12 correctness properties defined in the design.

## Tasks

- [x] 1. Event Bus and Core Infrastructure
  - [x] 1.1 Implement the in-process EventBus with per-session seq assignment and subscriber management
    - Implement `EventBus` class with `publish()`, `subscribe()`, `replay()` methods
    - Per-session `asyncio.Lock` for monotonic `seq` assignment starting at 1
    - Subscriber pattern matching (glob-style on event type)
    - At-least-once delivery with retry up to 5 attempts per subscriber
    - Idempotency enforcement on `(session_id, seq)` pairs
    - _Requirements: 17.1, 17.2, 17.3, 17.6, 17.7, 17.8_

  - [x] 1.2 Implement causation linking and event validation
    - Validate `causation_id` references an event with smaller `seq` in same session
    - Reject events missing required fields (`type`, `payload`, `correlation_id`, `source`, `event_id`, `timestamp`)
    - Leave `causation_id` unset for root events (no prior cause)
    - _Requirements: 17.4, 17.5, 17.2_

  - [x] 1.3 Implement streaming backpressure with bounded subscriber queues
    - Per-subscriber bounded queue (configurable max depth, default 1000)
    - Drop-oldest coalescible `token` events when queue is full
    - Never drop lifecycle events (`*.start`, `*.done`, `*.fail`, `task.done`, `build.done`, `question`, `error`)
    - Block producer up to configurable timeout (default 5s) if lifecycle event cannot fit; persist on timeout
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

  - [x] 1.4 Write property tests for event ordering and causality (Hypothesis)
    - **Property 4: Event ordering** — for all events sharing a `correlation_id`, `seq` values are unique and strictly increasing
    - **Property 5: Causality closure** — for every non-root event, `causation_id` references an event with smaller `seq` in same session
    - **Validates: Requirements 17.3, 17.4, 17.5**

  - [x] 1.5 Write unit tests for EventBus publish, subscribe, replay, and backpressure
    - Test sequential `seq` assignment across concurrent publishes
    - Test subscriber delivery retry on failure
    - Test idempotent duplicate rejection
    - Test backpressure queue overflow and token coalescence
    - _Requirements: 17.1, 17.3, 17.6, 17.7, 17.8, 18.1, 18.2, 18.3, 18.4_

- [x] 2. Capability Registry and Discovery
  - [x] 2.1 Implement the CapabilityRegistry with register/deregister/resolve operations
    - `register()`, `deregister()`, `get()`, `has()`, `has_kind()`, `any_for_role()`, `healthy_for_role()`, `summary()` methods
    - Raise `CapabilityUnavailableError` when requested capability not present
    - `asyncio.Lock` guarding mutations (only Discovery and Health Monitor write)
    - Emit `capability.registered` and `capability.deregistered` events on mutations
    - _Requirements: 15.1, 15.2, 14.5_

  - [x] 2.2 Implement the Discovery bootstrap procedure
    - Load and validate configuration YAML files (`models.yaml`, `policies.yaml`, `rate_limits.yaml`, `tools.yaml`, `verification.yaml`)
    - Halt startup within 1 second on schema validation errors
    - Probe all configured resources concurrently with per-probe timeout of 5 seconds
    - Register only healthy resources; emit `capability.degraded` for unhealthy ones
    - Emit `forge.boot.discovery_complete` with healthy/unhealthy sets
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 15.3, 15.4, 15.5_

  - [x] 2.3 Implement the HealthMonitor with continuous re-probing and degraded/recovered transitions
    - Re-probe every registered capability on configurable interval (bounded 5–300s, default 30s)
    - Per-probe timeout bounded 1–60s, default 10s
    - Consecutive failure threshold (bounded 1–10, default 3) before deregistration
    - Recovery threshold (bounded 1–10, default 2) consecutive healthy probes before re-registration
    - Emit `capability.degraded` / `capability.recovered` events on transitions
    - Write `DecisionRecord` on capability transitions
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

  - [x] 2.4 Implement minimum-capability validation and operational mode evaluation
    - Require Coder role + VCS connector + coding tool for OPERATIONAL
    - Enter DEGRADED mode with precise missing-capability reasons in CapabilitySummary
    - Record soft degradations (Reviewer, vector store) as DecisionRecord
    - Emit capability transition event and record on mode changes
    - Emit `forge.ready` with operational mode, CapabilitySummary, and startup report
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 13.7_

  - [x] 2.5 Write property test for discovery soundness (Hypothesis)
    - **Property 2: Discovery soundness** — for every capability in the registry after bootstrap, its last health_check returned ok=true; no unhealthy capability is ever registered
    - **Validates: Requirements 13.4, 13.5, 15.2**

  - [x] 2.6 Write unit tests for Registry, Discovery, HealthMonitor, and mode evaluation
    - Test register/deregister/resolve operations
    - Test concurrent probe timeout handling in discovery
    - Test consecutive failure counting and recovery transitions
    - Test DEGRADED vs OPERATIONAL mode evaluation
    - _Requirements: 13.1–13.7, 14.1–14.5, 15.1–15.5, 16.1–16.6_

- [x] 3. Checkpoint - Ensure event bus and registry tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Secret Handling and Session Budget
  - [x] 4.1 Implement the SecretHolder with per-session in-memory secret storage and redaction
    - Per-session store for VCS token and provider keys (plaintext only in memory)
    - Serialization hook that replaces raw secrets with masked placeholders before any persistence
    - Token reference stored in state snapshots instead of raw values
    - Clear all secrets on session termination
    - Abort persistence operations if redaction cannot be applied
    - _Requirements: 22.1, 22.2, 22.3, 22.5, 22.6, 22.7_

  - [x] 4.2 Implement SessionBudget with token/cost governance
    - Initialize per-session budget to configured token limit with consumed = 0
    - Pre-call estimation check: raise `BudgetExceededError` if estimated tokens exceed remaining
    - Post-call charge: add actual token usage to consumed amount
    - Emit `budget.exceeded` event when budget is exceeded
    - Expose limit, consumed, and remaining via RuntimeInspector
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [x] 4.3 Write property test for secret non-leakage (Hypothesis)
    - **Property 9: Secret non-leakage** — for all persisted artifacts (state snapshots, audit records, event payloads), no raw VCS token or provider key appears
    - **Validates: Requirements 22.1, 22.2, 22.3**

  - [x] 4.4 Write property test for budget safety (Hypothesis)
    - **Property 8: Budget safety** — no model call is issued when session.budget.remaining < estimated_tokens
    - **Validates: Requirements 12.2, 12.4**

  - [x] 4.5 Write unit tests for SecretHolder redaction and SessionBudget enforcement
    - Test secret clearing on session end
    - Test redaction in state snapshots and event payloads
    - Test budget exceeded rejection and post-call charging
    - _Requirements: 22.1–22.7, 12.1–12.5_

- [x] 5. Model Router with Fallback and Circuit Breaker
  - [x] 5.1 Implement the ModelRouter with role-based resolution and ordered fallback chain
    - Resolve role to ordered provider+model chain from `models.yaml`
    - Skip providers not present in Registry or with open circuit breaker
    - Attempt providers strictly in chain order
    - Return first healthy, breaker-closed success
    - Raise `ModelUnavailableError` after exhausting all providers with attempt log
    - Record attempt log in AuditTrail on exhaustion
    - _Requirements: 11.1, 11.2, 11.3, 11.6, 11.7_

  - [x] 5.2 Implement per-provider CircuitBreaker with cancellable backoff
    - Record success/failure on each provider attempt
    - Trip breaker after configured consecutive failures
    - Configurable reset window for breaker recovery
    - Cancellable exponential backoff (bounded 1–30s) on transient errors, max 3 retries per provider
    - Permanent errors (auth, bad-request) stop retrying that provider immediately
    - Per-call timeout of 60s; treat timeout as transient error
    - Cancel in-flight backoff on user interrupt within 2 seconds
    - _Requirements: 11.4, 11.5, 11.9, 24.7_

  - [x] 5.3 Implement model routing event emission and budget integration
    - Emit `model.selected` event on successful provider selection
    - Emit `model.fallback` event when falling back between providers
    - Events carry Role, provider, model, attempt, and reason
    - Integrate SessionBudget pre-call estimation check and post-call charge
    - Emit `budget.exceeded` event and record in AuditTrail on budget violation
    - _Requirements: 11.8, 12.2, 12.3, 12.4_

  - [x] 5.4 Write property tests for routing soundness and determinism (Hypothesis)
    - **Property 1: Routing soundness** — route() returns the completion from the first provider that is present in Registry, breaker-closed, and succeeds; never returns from unavailable or breaker-open provider
    - **Property 7: Router fallback monotonicity** — route() tries providers strictly in chain order, never retries after PermanentError, raises ModelUnavailableError only after exhausting chain
    - **Validates: Requirements 11.1, 11.2, 11.3, 11.6, 11.7**

  - [x] 5.5 Write unit tests for ModelRouter, CircuitBreaker, and budget integration
    - Test chain ordering and provider skipping
    - Test circuit breaker trip and recovery
    - Test cancellable backoff behavior
    - Test budget pre-check and post-charge
    - _Requirements: 11.1–11.9, 12.2–12.4_

- [x] 6. Checkpoint - Ensure secrets, budget, and router tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Session Lifecycle and Application Layer
  - [x] 7.1 Implement session CRUD (create, list, get, delete) with persistence
    - Create session with unique ID, initial status, Build_Mode from repo URL + goal
    - Store VCS token in SecretHolder, token reference in session state
    - List sessions with identifiers, repo URLs, build modes, and statuses
    - Get session by ID with status and operability
    - Delete session: stop in-progress build, destroy workspaces, remove session
    - Reject creation on empty/malformed repo URL or empty goal with validation error
    - Return not-found for non-existent session IDs on get/delete
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

  - [x] 7.2 Implement the Audit Trail as a persisted event-stream projection
    - Persist events to `audit_log` table with unique key on `(session_id, seq)`
    - Write `DecisionRecord` entries with kind, subject, inputs, decision, rationale, alternatives, caused_by_event_id
    - Deterministic rationale construction from inputs only (no AI prose)
    - Reject duplicate `(session_id, seq)` pairs (idempotent)
    - Retain originating event on write failure; surface audit-write error
    - _Requirements: 19.1, 19.2, 19.3, 19.5, 19.6_

  - [x] 7.3 Write property test for audit replay fidelity (Hypothesis)
    - **Property 6: Audit replay fidelity** — replaying audit_log ordered by seq reconstructs a ForgeState whose persisted fields are field-for-field equal to the original
    - **Validates: Requirements 19.3, 19.4**

  - [x] 7.4 Write unit tests for session lifecycle and audit trail
    - Test session create with valid/invalid inputs
    - Test session delete with in-progress build
    - Test audit record persistence and deduplication
    - Test deterministic rationale construction
    - _Requirements: 1.1–1.8, 19.1–19.6_

- [x] 8. Deterministic Intent Classification and Clarification
  - [x] 8.1 Implement the fast-path deterministic classifier
    - Rules-based classifier for interrupt, pause, resume, status, and structured commands
    - Classify message into exactly one intent class from message content + build state
    - No AI model invoked for matched commands
    - Fall back to Interrupt_Handler role for unmatched natural language
    - Return classification-unavailable if no provider can serve Interrupt_Handler role
    - Same message + same build state = same intent class (determinism guarantee)
    - _Requirements: 2.1, 2.2, 2.3, 2.6, 2.7, 2.8_

  - [x] 8.2 Implement intent routing (status_query and build_intent)
    - Route `status_query` to RuntimeInspector without AI model
    - Route `build_intent` into build workflow
    - Treat interrupt intent as non-build when no build is in progress
    - _Requirements: 2.4, 2.5, 2.6_

  - [x] 8.3 Implement clarification workflow with SessionContext
    - Emit clarification question events for missing specification inputs (up to configured max)
    - Record answers and constraints into SessionContext before advancing
    - Fixed declared field order for deterministic serialization of context into prompts
    - Include recorded constraints in all subsequent prompt-constructing nodes
    - Proceed without questions when all required inputs are present
    - Re-emit question on empty answer; do not advance workflow
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 8.4 Write unit tests for intent classification and clarification
    - Test fast-path matching for each command type
    - Test fallback to AI classifier
    - Test deterministic classification with fixed state
    - Test clarification question emission and answer recording
    - Test empty answer rejection
    - _Requirements: 2.1–2.8, 3.1–3.6_

- [ ] 9. Specification, Planning, and Task Dependency Graph
  - [x] 9.1 Implement specification and task generation via Architect role
    - Invoke Architect role exactly once after clarification completes
    - Generate specification artifact and task list from Digital Twin + SessionContext
    - Save artifact to Artifact Store; emit `spec.ready` with URI and version
    - Emit `tasks.ready` with task count and graph reference
    - Error handling: emit error event on generation or save failure; do not advance
    - Return not-found for session with no specification
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [x] 9.2 Implement task dependency planning with DAG construction
    - Construct directed dependency graph from task list
    - Produce topological ordering when graph is acyclic
    - Report cycle error with task identifiers forming the cycle
    - Report unresolved dependency error for missing task references
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 9.3 Write property test for plan acyclicity (Hypothesis)
    - **Property 5: Plan acyclicity** — for all task sets, `plan` produces a DAG or reports a cycle; every `depends_on` references an existing task id
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**

  - [x] 9.4 Write unit tests for specification generation and planning
    - Test Architect invocation and artifact save
    - Test event emission on success and failure
    - Test cycle detection in dependency graph
    - Test unresolved dependency identification
    - _Requirements: 4.1–4.7, 5.1–5.4_

- [x] 10. Checkpoint - Ensure classification, clarification, and planning tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Workspace Manager and Task Dispatcher
  - [x] 11.1 Implement the WorkspaceManager with create/destroy/merge operations
    - Create sandboxed copy of repository at session base ref
    - Emit `workspace.created` event with workspace ID and owning task ID
    - Emit workspace-creation-failure event on error; block task execution
    - Destroy workspace on task completion or merge; emit `workspace.destroyed`
    - Reap orphaned workspaces exceeding configured max age; emit destroy with orphan-expiry reason
    - _Requirements: 6.1, 6.2, 6.3, 6.6, 6.7_

  - [x] 11.2 Implement the TaskDispatcher with isolation and sequential execution
    - Request isolated workspace before any file-system write
    - Confine all task file-system writes to assigned workspace
    - Execute ready tasks one at a time in dependency order (parallelism=1)
    - Never write to canonical repository during task execution
    - _Requirements: 6.1, 6.4, 6.5_

  - [x] 11.3 Write property test for workspace isolation (Hypothesis)
    - **Property 9: Workspace isolation** — no worker writes to the canonical repository; all task work happens in an isolated workspace and reaches canonical repo only via commit/merge
    - **Validates: Requirements 6.4, 6.5**

  - [x] 11.4 Write unit tests for WorkspaceManager and TaskDispatcher
    - Test workspace creation and destruction lifecycle
    - Test orphaned workspace reaping
    - Test task confinement to assigned workspace
    - Test sequential execution ordering
    - _Requirements: 6.1–6.7_

- [x] 12. Verification Pipeline and Policy Engine
  - [x] 12.1 Implement the VerificationPipeline with advisory and blocking stages
    - Run all enabled advisory verifier stages concurrently with per-stage timeout
    - Record failed/timed-out advisory stages; continue without halting
    - Merge advisory results into deterministic map keyed by stage name (order-independent)
    - Produce empty advisory map when no advisory stages enabled
    - Run blocking gate stages in declared order after advisory merge
    - Halt pipeline on blocking failure; retain workspace; request Policy decision
    - Emit `verify.stage` event per stage (passed/failed/timed-out)
    - Emit `verify.passed` when all blocking stages pass
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8_

  - [x] 12.2 Implement the PolicyEngine with retry/escalate/skip decisions
    - Deterministically decide one of retry, escalate, or skip from rules + attempt count
    - Emit exactly one `policy.decision` event per decision
    - Write DecisionRecord for each decision
    - Escalate to alternate coding tool on tool failure threshold
    - Mark task failed if escalation target unavailable in Registry
    - On retry budget exhaustion: mark task failed, continue independent tasks, block dependent tasks
    - Emit `task.fail` event with task ID, failing stage, reason, and policy decision
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [x] 12.3 Write property test for verification merge order-independence (Hypothesis)
    - **Property 3: Verification merge order-independence** — for all sets of advisory verifier results, the merged dict[stage_name -> result] is independent of wall-clock arrival order
    - **Validates: Requirements 7.3, 7.4**

  - [x] 12.4 Write unit tests for VerificationPipeline and PolicyEngine
    - Test concurrent advisory stage execution with timeouts
    - Test blocking gate halt behavior
    - Test policy decision routing (retry/escalate/skip)
    - Test retry budget exhaustion
    - _Requirements: 7.1–7.8, 8.1–8.7_

- [x] 13. Checkpoint - Ensure workspace, verification, and policy tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 14. Commit, Finalization, and Documentation
  - [x] 14.1 Implement commit workflow with approval gates
    - Commit task changes from workspace to canonical repo on `verify.passed`
    - Emit `commit.done` with commit SHA and changed file paths
    - Never commit without `verify.passed`; reject uncommitted tasks
    - Approval-gated actions: pause, request approval, emit pending event
    - Handle commit failure: leave repo unchanged, emit error event
    - _Requirements: 9.1, 9.2, 9.4, 9.5_

  - [x] 14.2 Implement finalization with push and build summary
    - Push canonical repo to VCS after all tasks processed
    - Emit `build.done` with summary, committed tasks, and skipped tasks
    - Handle push failure: retain commits, emit error, do not emit `build.done`
    - Handle approval timeout/rejection: do not perform action, emit event
    - _Requirements: 9.3, 9.6, 9.7_

  - [-] 14.3 Implement Digital Twin diff and documentation maintenance
    - Compute twin diff (modules added/changed/removed) on `commit.done`
    - DocWriter updates README and `docs` files; emits `doc_updated` event
    - On DocWriter failure: leave file unchanged, record drift entry, emit event
    - After finalization: update docs or record drift for each changed module
    - `document` build mode: diff all docs against twin, update mismatched files
    - Skip files already matching twin in document mode
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [-] 14.4 Write property test for documentation non-drift (Hypothesis)
    - **Property 10: Documentation non-drift** — after finalize, for every module changed in twin diff, either docs were updated or a DocDrift entry is recorded
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4**

  - [-] 14.5 Write unit tests for commit, finalization, and documentation
    - Test commit on verify.passed and rejection without it
    - Test approval gate pause and timeout behavior
    - Test twin diff computation and doc update
    - Test drift recording on DocWriter failure
    - _Requirements: 9.1–9.7, 10.1–10.6_

- [ ] 15. Runtime Inspector and Crash Recovery
  - [~] 15.1 Implement the RuntimeInspector as a query-only facade
    - Return current node, worker status, task queue, active task from runtime state (within 2s)
    - Return empty active task and empty queue when no build in progress
    - Return most recent DecisionRecord for session (kind, subject, inputs, decision, rationale, alternatives)
    - Return not-found when no DecisionRecord or task exists
    - Reconstruct task history via causation links (model selections, verifier outcomes, retries, commit)
    - Derive all responses from runtime state, AuditTrail, and CapabilityRegistry only (no AI)
    - _Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6, 20.7_

  - [~] 15.2 Implement crash recovery and resume workflow
    - Checkpoint redacted ForgeState, completed node ID, and highest seq to relational store after each node
    - Halt on checkpoint write failure; emit error; do not advance
    - On restart: identify sessions with non-terminal checkpoint; resume from last completed node
    - Destroy workspace and re-queue task if no `commit.done` at crash time (within retry budget)
    - Replay missed events on client reconnect in seq order
    - Send state snapshot + retained events if client's last seq precedes oldest retained event
    - _Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6_

  - [~] 15.3 Write property test for explainability without inference (Hypothesis)
    - **Property 8: Explainability without inference** — every explain and runtime response is derived solely from structured state (audit trail, registry, ForgeState); no LLM invoked
    - **Validates: Requirements 20.7, 12.5**

  - [~] 15.4 Write unit tests for RuntimeInspector and crash recovery
    - Test inspector responses with and without active builds
    - Test DecisionRecord retrieval and not-found cases
    - Test checkpoint persistence and resume on restart
    - Test workspace destruction on crash recovery
    - _Requirements: 20.1–20.7, 21.1–21.6_

- [ ] 16. Interrupt Handling and Outcome Recording
  - [~] 16.1 Implement interrupt, pause, resume, redirect, and stop
    - Pause execution within 2 seconds of interrupt; retain all build state
    - Surface interrupt message to developer within 2 seconds of pausing
    - Resume from paused point using retained state; emit `interrupt.resumed` within 2s
    - Reject resume when no interrupt is active; return error
    - Redirect: return to planning seeded with new direction (non-empty)
    - Reject redirect with empty direction; keep paused state
    - Cancel in-flight model backoff within 2 seconds on stop/interrupt
    - _Requirements: 24.1, 24.2, 24.3, 24.4, 24.5, 24.6, 24.7_

  - [~] 16.2 Implement the LearningRecorder for build outcome recording
    - Record one outcome entry per executed task on build finalize
    - Entry includes: task type, tool, model, role, outcome status (success/failure), retry count, escalation flag
    - Persist entries to relational store
    - Complete build without discarding result on recording failure; emit failure event
    - Return recommendations derived from recorded outcomes on request
    - Never apply recommendations automatically
    - _Requirements: 25.1, 25.2, 25.3, 25.4, 25.5_

  - [~] 16.3 Write unit tests for interrupt handling and outcome recording
    - Test pause/resume/redirect/stop flows
    - Test timing constraints (2-second response)
    - Test outcome recording with success and failure statuses
    - Test recording failure handling
    - _Requirements: 24.1–24.7, 25.1–25.5_

- [~] 17. Checkpoint - Ensure inspector, recovery, interrupts, and recording tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 18. API Endpoints and Layer Boundaries
  - [~] 18.1 Implement REST and WebSocket endpoints
    - Session management endpoints (CRUD)
    - Messaging endpoint for developer messages
    - Artifact retrieval endpoints
    - Control endpoints (interrupt, resume, redirect, stop)
    - Runtime inspection endpoints (status, explain, capabilities)
    - WebSocket event stream per session: forward events in seq order, no engineering logic
    - Reject stream connection for non-existent session with not-found error
    - Serve explain/runtime responses from RuntimeInspector only (no AI)
    - _Requirements: 26.1, 26.2, 26.3, 26.4_

  - [~] 18.2 Implement authentication and authorization
    - Require valid auth credentials on all endpoints and WebSocket streams
    - Reject requests with missing/invalid credentials with authentication-failure response
    - Do not perform requested operation on auth failure
    - _Requirements: 26.5, 26.6_

  - [~] 18.3 Implement and enforce layer boundary checks
    - Verify module imports target own layer or exactly one adjacent layer
    - Presentation layer: only runtime API + event catalog, no orchestration logic
    - Application layer: transport translation only, no engineering logic
    - Runtime layer: protocol interfaces only, no HTTP/transport logic
    - Adapter layer: one protocol call to one infrastructure call, no business logic
    - Fail boundary check with error identifying offending import path
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6_

  - [~] 18.4 Write unit tests for API endpoints and layer boundaries
    - Test REST endpoint responses for valid/invalid requests
    - Test WebSocket stream ordering and rejection
    - Test authentication enforcement
    - Test layer boundary violation detection
    - _Requirements: 26.1–26.6, 23.1–23.6_

- [~] 19. Final Checkpoint - Full integration verification
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at natural boundaries
- Property tests validate universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases
- The implementation language is Python (asyncio/FastAPI/LangGraph) matching the existing codebase
- All property tests reference design correctness properties by number
- The implementation builds on existing foundation: `app/runtime/models.py`, `protocols.py`, `types.py`, and `events/`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.2"] },
    { "id": 2, "tasks": ["1.4", "1.5", "2.3", "2.4"] },
    { "id": 3, "tasks": ["2.5", "2.6"] },
    { "id": 4, "tasks": ["4.1", "4.2"] },
    { "id": 5, "tasks": ["4.3", "4.4", "4.5", "5.1"] },
    { "id": 6, "tasks": ["5.2", "5.3"] },
    { "id": 7, "tasks": ["5.4", "5.5"] },
    { "id": 8, "tasks": ["7.1", "7.2"] },
    { "id": 9, "tasks": ["7.3", "7.4", "8.1"] },
    { "id": 10, "tasks": ["8.2", "8.3"] },
    { "id": 11, "tasks": ["8.4", "9.1"] },
    { "id": 12, "tasks": ["9.2", "9.3", "9.4"] },
    { "id": 13, "tasks": ["11.1", "11.2"] },
    { "id": 14, "tasks": ["11.3", "11.4", "12.1"] },
    { "id": 15, "tasks": ["12.2", "12.3", "12.4"] },
    { "id": 16, "tasks": ["14.1", "14.2"] },
    { "id": 17, "tasks": ["14.3", "14.4", "14.5"] },
    { "id": 18, "tasks": ["15.1", "15.2"] },
    { "id": 19, "tasks": ["15.3", "15.4", "16.1"] },
    { "id": 20, "tasks": ["16.2", "16.3"] },
    { "id": 21, "tasks": ["18.1", "18.2"] },
    { "id": 22, "tasks": ["18.3", "18.4"] }
  ]
}
```
