# Implementation Plan: Forge Runtime

## Overview

This plan implements the Forge autonomous software engineering runtime — a Python asyncio/FastAPI/LangGraph system that orchestrates builds across isolated workspaces with typed events, health-aware model routing, staged verification, and explainable audit trails. The implementation builds incrementally on the existing foundation (models, protocols, event types, types) and targets all 26 requirements and 12 correctness properties defined in the design.

## Tasks

- [ ] 1. Event Bus and Core Infrastructure
  - [ ] 1.1 Implement the in-process EventBus with per-session seq assignment and subscriber management
    - Implement `EventBus` class with `publish()`, `subscribe()`, `replay()` methods
    - Per-session `asyncio.Lock` for monotonic `seq` assignment starting at 1
    - Subscriber pattern matching (glob-style on event type)
    - At-least-once delivery with retry up to 5 attempts per subscriber
    - Idempotency enforcement on `(session_id, seq)` pairs
    - _Requirements: 17.1, 17.2, 17.3, 17.6, 17.7, 17.8_

  - [ ] 1.2 Implement causation linking and event validation
    - Validate `causation_id` references an event with smaller `seq` in same session
    - Reject events missing required fields (`type`, `payload`, `correlation_id`, `source`, `event_id`, `timestamp`)
    - Leave `causation_id` unset for root events (no prior cause)
    - _Requirements: 17.4, 17.5, 17.2_

  - [ ] 1.3 Implement streaming backpressure with bounded subscriber queues
    - Per-subscriber bounded queue (configurable max depth, default 1000)
    - Drop-oldest coalescible `token` events when queue is full
    - Never drop lifecycle events (`*.start`, `*.done`, `*.fail`, `task.done`, `build.done`, `question`, `error`)
    - Block producer up to configurable timeout (default 5s) if lifecycle event cannot fit; persist on timeout
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

  - [ ]* 1.4 Write property tests for event ordering and causality (Hypothesis)
    - **Property 4: Event ordering** — for all events sharing a `correlation_id`, `seq` values are unique and strictly increasing
    - **Property 5: Causality closure** — for every non-root event, `causation_id` references an event with smaller `seq` in same session
    - **Validates: Requirements 17.3, 17.4, 17.5**

  - [ ]* 1.5 Write unit tests for EventBus publish, subscribe, replay, and backpressure
    - Test sequential `seq` assignment across concurrent publishes
    - Test subscriber delivery retry on failure
    - Test idempotent duplicate rejection
    - Test backpressure queue overflow and token coalescence
    - _Requirements: 17.1, 17.3, 17.6, 17.7, 17.8, 18.1, 18.2, 18.3, 18.4_

- [ ] 2. Capability Registry and Discovery
  - [ ] 2.1 Implement the CapabilityRegistry with register/deregister/resolve operations
    - `register()`, `deregister()`, `get()`, `has()`, `has_kind()`, `any_for_role()`, `healthy_for_role()`, `summary()` methods
    - Raise `CapabilityUnavailableError` when requested capability not present
    - `asyncio.Lock` guarding mutations (only Discovery and Health Monitor write)
    - Emit `capability.registered` and `capability.deregistered` events on mutations
    - _Requirements: 15.1, 15.2, 14.5_

  - [ ] 2.2 Implement the Discovery bootstrap procedure
    - Load and validate configuration YAML files (`models.yaml`, `policies.yaml`, `rate_limits.yaml`, `tools.yaml`, `verification.yaml`)
    - Halt startup within 1 second on schema validation errors
    - Probe all configured resources concurrently with per-probe timeout of 5 seconds
    - Register only healthy resources; emit `capability.degraded` for unhealthy ones
    - Emit `forge.boot.discovery_complete` with healthy/unhealthy sets
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 15.3, 15.4, 15.5_

  - [ ] 2.3 Implement the HealthMonitor with continuous re-probing and degraded/recovered transitions
    - Re-probe every registered capability on configurable interval (bounded 5–300s, default 30s)
    - Per-probe timeout bounded 1–60s, default 10s
    - Consecutive failure threshold (bounded 1–10, default 3) before deregistration
    - Recovery threshold (bounded 1–10, default 2) consecutive healthy probes before re-registration
    - Emit `capability.degraded` / `capability.recovered` events on transitions
    - Write `DecisionRecord` on capability transitions
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

  - [ ] 2.4 Implement minimum-capability validation and operational mode evaluation
    - Require Coder role + VCS connector + coding tool for OPERATIONAL
    - Enter DEGRADED mode with precise missing-capability reasons in CapabilitySummary
    - Record soft degradations (Reviewer, vector store) as DecisionRecord
    - Emit capability transition event and record on mode changes
    - Emit `forge.ready` with operational mode, CapabilitySummary, and startup report
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 13.7_

  - [ ]* 2.5 Write property test for discovery soundness (Hypothesis)
    - **Property 2: Discovery soundness** — for every capability in the registry after bootstrap, its last health_check returned ok=true; no unhealthy capability is ever registered
    - **Validates: Requirements 13.4, 13.5, 15.2**

  - [ ]* 2.6 Write unit tests for Registry, Discovery, HealthMonitor, and mode evaluation
    - Test register/deregister/resolve operations
    - Test concurrent probe timeout handling in discovery
    - Test consecutive failure counting and recovery transitions
    - Test DEGRADED vs OPERATIONAL mode evaluation
    - _Requirements: 13.1–13.7, 14.1–14.5, 15.1–15.5, 16.1–16.6_

- [ ] 3. Checkpoint - Ensure event bus and registry tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Secret Handling and Session Budget
  - [ ] 4.1 Implement the SecretHolder with per-session in-memory secret storage and redaction
    - Per-session store for VCS token and provider keys (plaintext only in memory)
    - Serialization hook that replaces raw secrets with masked placeholders before any persistence
    - Token reference stored in state snapshots instead of raw values
    - Clear all secrets on session termination
    - Abort persistence operations if redaction cannot be applied
    - _Requirements: 22.1, 22.2, 22.3, 22.5, 22.6, 22.7_

  - [ ] 4.2 Implement SessionBudget with token/cost governance
    - Initialize per-session budget to configured token limit with consumed = 0
    - Pre-call estimation check: raise `BudgetExceededError` if estimated tokens exceed remaining
    - Post-call charge: add actual token usage to consumed amount
    - Emit `budget.exceeded` event when budget is exceeded
    - Expose limit, consumed, and remaining via RuntimeInspector
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [ ]* 4.3 Write property test for secret non-leakage (Hypothesis)
    - **Property 9: Secret non-leakage** — for all persisted artifacts (state snapshots, audit records, event payloads), no raw VCS token or provider key appears
    - **Validates: Requirements 22.1, 22.2, 22.3**

  - [ ]* 4.4 Write property test for budget safety (Hypothesis)
    - **Property 8: Budget safety** — no model call is issued when session.budget.remaining < estimated_tokens
    - **Validates: Requirements 12.2, 12.4**

  - [ ]* 4.5 Write unit tests for SecretHolder redaction and SessionBudget enforcement
    - Test secret clearing on session end
    - Test redaction in state snapshots and event payloads
    - Test budget exceeded rejection and post-call charging
    - _Requirements: 22.1–22.7, 12.1–12.5_

- [ ] 5. Model Router with Fallback and Circuit Breaker
  - [ ] 5.1 Implement the ModelRouter with role-based resolution and ordered fallback chain
    - Resolve role to ordered provider+model chain from `models.yaml`
    - Skip providers not present in Registry or with open circuit breaker
    - Attempt providers strictly in chain order
    - Return first healthy, breaker-closed success
    - Raise `ModelUnavailableError` after exhausting all providers with attempt log
    - Record attempt log in AuditTrail on exhaustion
    - _Requirements: 11.1, 11.2, 11.3, 11.6, 11.7_

  - [ ] 5.2 Implement per-provider CircuitBreaker with cancellable backoff
    - Record success/failure on each provider attempt
    - Trip breaker after configured consecutive failures
    - Configurable reset window for breaker recovery
    - Cancellable exponential backoff (bounded 1–30s) on transient errors, max 3 retries per provider
    - Permanent errors (auth, bad-request) stop retrying that provider immediately
    - Per-call timeout of 60s; treat timeout as transient error
    - Cancel in-flight backoff on user interrupt within 2 seconds
    - _Requirements: 11.4, 11.5, 11.9, 24.7_

  - [ ] 5.3 Implement model routing event emission and budget integration
    - Emit `model.selected` event on successful provider selection
    - Emit `model.fallback` event when falling back between providers
    - Events carry Role, provider, model, attempt, and reason
    - Integrate SessionBudget pre-call estimation check and post-call charge
    - Emit `budget.exceeded` event and record in AuditTrail on budget violation
    - _Requirements: 11.8, 12.2, 12.3, 12.4_

  - [ ]* 5.4 Write property tests for routing soundness and determinism (Hypothesis)
    - **Property 7: Router fallback monotonicity** — route() tries providers strictly in chain order, never retries after PermanentError, raises ModelUnavailableError only after exhausting chain
    - **Validates: Requirements 11.1, 11.2, 11.6, 11.7**

  - [ ]* 5.5 Write unit tests for ModelRouter, CircuitBreaker, and budget integration
    - Test chain ordering and provider skipping
    - Test circuit breaker trip and recovery
    - Test cancellable backoff behavior
    - Test budget pre-check and post-charge
    - _Requirements: 11.1–11.9, 12.2–12.4_
