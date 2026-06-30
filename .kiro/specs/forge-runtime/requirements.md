# Requirements Document

## Introduction

Forge is an autonomous software engineering runtime. A developer supplies a GitHub repository URL and a plain-English goal; Forge plans, builds, reviews, verifies, and commits code into that repository, streaming every decision and artifact back through a chat interface in real time.

These requirements are derived from the approved Forge Runtime design document. They capture the behavior the system must exhibit so that it satisfies the six guiding principles: Deterministic, Observable, Modular, Replaceable, Explainable, and Resilient. The requirements treat the product vision as fixed and reflect the design's revised v1 scope: a single-process asyncio core, an in-process typed event bus as the single source of truth, sequential task execution behind a parallelism seam, a lean Digital Twin, a concrete relational persistence layer with pluggable artifact/vector stores, and explicit additions for causal events, an audit schema, secret redaction, crash recovery, streaming backpressure, and cost governance.

## Glossary

- **Forge_Runtime**: The autonomous orchestration core that owns all state, emits all events, and makes and records all decisions. Layer 3 of the architecture.
- **Application_Layer**: The FastAPI HTTP/WebSocket boundary that creates sessions, ingests messages, triggers the workflow, and broadcasts events. Contains no engineering logic.
- **Presentation_Layer**: The web client (Next.js) that renders chat, spec, task checklist, verification stages, and commit log from streamed events.
- **Adapter_Layer**: Concrete implementations of plugin protocols (AI provider, coding tool, VCS, artifact store, vector store, verifier, doc writer).
- **Workflow_Engine**: The LangGraph state machine that drives a build over a `ForgeState` object through ordered nodes (intake, clarify, architect, plan, execute, verify, commit, doc_update, finalize, interrupt).
- **Event_Bus**: The in-process, typed publish/subscribe channel; the single source of truth for everything that happens in a session.
- **Event**: A typed envelope carrying `type`, `payload`, `correlation_id` (session id), `causation_id`, `source`, monotonic per-session `seq`, `event_id`, and `timestamp`.
- **Capability_Registry**: The authoritative record of which capabilities are available and healthy at the current instant; resolves capabilities by name and by role.
- **Discovery_Procedure**: A bootstrap-time procedure that probes all configured resources in parallel and registers only healthy ones.
- **Health_Monitor**: The background service that continuously re-probes resources and flips availability, emitting degraded/recovered transitions.
- **Capability**: A named, discoverable unit of functionality (an AI provider, coding tool, VCS connector, store, vector store, verifier, or runtime resource).
- **Role**: An abstract AI function (Clarification, Architect, Planner, Coder, Reviewer, Doc_Writer, Interrupt_Handler) resolved to a concrete provider+model by the Model Router.
- **Model_Router**: The component that resolves a Role to a healthy provider+model using an ordered fallback chain, a per-provider circuit breaker, and a per-session budget.
- **Circuit_Breaker**: Per-provider state that, when open, causes the Model Router to skip that provider immediately rather than attempt it.
- **Task_Dispatcher**: The component that assigns ready tasks to workers and workspaces, honoring the dependency graph and the parallelism policy.
- **Workspace_Manager**: The component that creates, destroys, and merges isolated sandboxed repository copies, one per task.
- **Workspace**: An isolated sandboxed copy of the repository at a base ref in which a single task's work is performed.
- **Verification_Pipeline**: The staged verifier runner; advisory read-only stages run concurrently, the blocking gate (tests then LLM review) runs strictly in declared order.
- **Verifier**: A plugin that checks task output; each declares whether it is `blocking` or `advisory`.
- **Policy_Engine**: The YAML-driven rules component that decides retry, escalate, or skip on verification failure, bounded by a retry budget.
- **Session_Context**: Persistent per-session working memory (goals, decisions, assumptions, constraints, preferences) injected into prompt-constructing nodes.
- **Digital_Twin**: A lean structured model of the repository (language, framework, file index with roles, git summary, entry points, documentation state).
- **Documentation_State**: The Digital Twin's first-class model of documentation files, their code references, last-synced commit SHA, and detected drift.
- **Doc_Writer**: The capability that updates README and `docs/*` files from a computed twin diff.
- **Audit_Trail**: The persisted projection of the event stream plus the `DecisionRecord` store; the backing store for explainability and resume.
- **Decision_Record**: A typed audit record capturing a non-trivial decision's kind, subject, inputs, decision, deterministic rationale, alternatives, and causing event.
- **Runtime_Inspector**: A query-only facade that answers "what" and "why" from runtime state, the Audit Trail, and the Registry, never by invoking an AI model.
- **Learning_Recorder**: The component that records build outcomes for later analysis; it surfaces recommendations but never auto-applies them.
- **Secret_Holder**: A per-session in-memory store for the VCS token and provider keys, redacted at every serialization boundary.
- **Session_Budget**: The per-session token/cost limit enforced by the Model Router.
- **Capability_Summary**: A snapshot of available, degraded, and missing-required capabilities plus an operability flag.
- **Operational_Mode**: The runtime's mode: OPERATIONAL (builds allowed) or DEGRADED (builds refused with reasons).
- **Build_Mode**: The kind of build requested: `new`, `extend`, `analyze`, or `document`.

## Requirements

### Requirement 1: Session Lifecycle Management

**User Story:** As a developer, I want to create and manage build sessions tied to a repository and a goal, so that each build is isolated and addressable.

#### Acceptance Criteria

1. WHEN a developer submits a non-empty repository URL and a non-empty plain-English goal, THE Application_Layer SHALL create a session with a unique session identifier, assign it an initial status and a Build_Mode, and persist it to the relational store.
2. WHEN a session is created, THE Application_Layer SHALL store the supplied VCS token in the per-session Secret_Holder and SHALL store only a token reference in the session state.
3. WHEN a developer requests the list of sessions, THE Application_Layer SHALL return all sessions with their identifiers, repository URLs, build modes, and statuses, and SHALL return an empty list when no sessions exist.
4. WHEN a developer requests an existing session by identifier, THE Application_Layer SHALL return that session's detail including status and operability.
5. WHEN a developer deletes an existing session, THE Application_Layer SHALL remove the session and SHALL destroy every Workspace associated with that session.
6. IF a requested session identifier does not exist for a get or delete request, THEN THE Application_Layer SHALL return a not-found response with the requested identifier and SHALL NOT modify any persisted state.
7. IF a session creation request carries an empty or malformed repository URL or an empty goal, THEN THE Application_Layer SHALL reject the request with a validation error identifying the invalid field and SHALL NOT create a session.
8. WHEN a developer deletes a session whose build is in progress, THE Application_Layer SHALL stop the in-progress build before removing the session and destroying its Workspaces.

### Requirement 2: Deterministic Intent Classification

**User Story:** As a developer, I want every message I send to be classified reliably, so that control commands like "stop" never depend on a reachable AI model.

#### Acceptance Criteria

1. WHEN a message is received, THE Workflow_Engine SHALL classify the message into exactly one intent class using the deterministic fast-path classifier, derived solely from the message content and the current build state, before invoking any AI model.
2. IF the fast-path classifier matches an interrupt, pause, resume, status, or structured command, THEN THE Workflow_Engine SHALL assign the intent class corresponding to that matched command without invoking an AI model.
3. WHEN the fast-path classifier does not match, THE Workflow_Engine SHALL invoke the Interrupt_Handler role to classify the message as natural language.
4. WHEN the intent class is `status_query`, THE Workflow_Engine SHALL answer using the Runtime_Inspector without invoking an AI model.
5. WHEN the intent class is `build_intent`, THE Workflow_Engine SHALL route the message into the build workflow.
6. WHILE no build is in progress, IF an `interrupt` intent is classified, THEN THE Workflow_Engine SHALL treat the message as a non-build message rather than an interrupt.
7. WHILE the fast-path classifier configuration is held fixed, THE Workflow_Engine SHALL assign the same intent class for identical messages received in the same build state.
8. IF the fast-path classifier does not match and no provider can serve the Interrupt_Handler role, THEN THE Workflow_Engine SHALL return a classification-unavailable indication and SHALL preserve the in-progress build state.

### Requirement 3: Clarification of Build Goals

**User Story:** As a developer, I want Forge to ask clarifying questions before building, so that the resulting specification matches my intent.

#### Acceptance Criteria

1. WHEN a build intent is missing one or more required specification inputs, THE Workflow_Engine SHALL emit a clarification question event for each missing input, up to the configured maximum number of clarification questions per build intent.
2. WHEN a developer answers a clarification question, THE Workflow_Engine SHALL record the answer and any stated constraints into the Session_Context before advancing the workflow.
3. THE Session_Context SHALL render its goals, decisions, assumptions, constraints, and preferences into prompts using a fixed declared field order that produces identical serialized output for identical context.
4. WHEN a developer states a constraint that is recorded in the Session_Context, THE Workflow_Engine SHALL include that constraint in every subsequent prompt-constructing node within the session.
5. WHEN all required specification inputs are present, THE Workflow_Engine SHALL proceed without emitting a clarification question.
6. IF a developer submits an empty answer to a clarification question, THEN THE Workflow_Engine SHALL re-emit the question, SHALL NOT advance the workflow, and SHALL indicate that an answer is required.

### Requirement 4: Specification and Task Generation

**User Story:** As a developer, I want Forge to generate a specification and a task list from my goal, so that I can see the plan before code is written.

#### Acceptance Criteria

1. WHEN clarification is complete, THE Workflow_Engine SHALL invoke the Architect role exactly once to generate one specification artifact and one task list from the Digital_Twin and the Session_Context before advancing to planning.
2. WHEN the Architect role produces a specification artifact, THE Workflow_Engine SHALL save the artifact to the Artifact Store and, upon a successful save, SHALL emit a `spec.ready` event carrying the artifact URI and a version that is unique and strictly increasing per session.
3. WHEN the Architect role produces a task list, THE Workflow_Engine SHALL emit a `tasks.ready` event carrying the task count and a task graph reference.
4. WHEN a developer requests the current specification AND a specification artifact exists for that session, THE Application_Layer SHALL return the most recent specification artifact for that session.
5. IF the Architect role fails to produce a specification artifact or a task list, THEN THE Workflow_Engine SHALL emit an error event indicating the generation failure, SHALL NOT advance to planning, and SHALL preserve the Session_Context.
6. IF saving the specification artifact to the Artifact Store fails, THEN THE Workflow_Engine SHALL NOT emit a `spec.ready` event and SHALL emit an error event indicating the save failure.
7. IF a developer requests the current specification and no specification artifact exists for that session, THEN THE Application_Layer SHALL return a not-found response identifying the session.

### Requirement 5: Task Dependency Planning

**User Story:** As a developer, I want tasks ordered by their dependencies, so that work proceeds in a valid sequence.

#### Acceptance Criteria

1. WHEN a task list is planned, THE Workflow_Engine SHALL construct a directed task dependency graph in which each node represents one task in the task set and each edge represents a declared dependency between two tasks.
2. WHEN the constructed dependency graph contains no cycles, THE Workflow_Engine SHALL produce a topological ordering in which every task appears only after all tasks it depends on, and SHALL make this ordering available as the planning output before execution begins.
3. IF the task dependencies contain a cycle, THEN THE Workflow_Engine SHALL produce an error result that identifies the set of task identifiers forming the detected cycle, SHALL NOT produce a task ordering, and SHALL NOT proceed to execution.
4. IF a task dependency references a task identifier that does not exist within the same task set, THEN THE Workflow_Engine SHALL produce an error result that identifies the referencing task identifier and the unresolved dependency identifier, and SHALL NOT proceed to execution.

### Requirement 6: Isolated Task Execution

**User Story:** As a developer, I want each coding task to run in an isolated sandbox, so that failures and retries never corrupt my repository.

#### Acceptance Criteria

1. WHEN a task becomes ready for execution, THE Task_Dispatcher SHALL request an isolated Workspace from the Workspace_Manager before performing any file-system write on behalf of that task.
2. WHEN a Workspace is requested, THE Workspace_Manager SHALL create a sandboxed copy of the repository at the session base ref recorded at session creation and SHALL emit a `workspace.created` event carrying the workspace identifier and the owning task identifier.
3. IF the Workspace_Manager cannot create the sandboxed copy for a requested Workspace, THEN THE Workspace_Manager SHALL emit a workspace-creation-failure event indicating the failure reason, and THE Task_Dispatcher SHALL NOT begin execution of that task.
4. WHILE a task is executing, THE Task_Dispatcher SHALL confine all of the task's file-system writes to that task's assigned Workspace and SHALL NOT write to the canonical repository.
5. WHILE the parallelism policy specifies a worker pool size of one, THE Task_Dispatcher SHALL execute ready tasks one at a time in dependency order.
6. WHEN a task completes or its Workspace is merged, THE Workspace_Manager SHALL destroy that Workspace exactly once and SHALL emit a `workspace.destroyed` event carrying the workspace identifier.
7. WHEN an orphaned Workspace, defined as a Workspace whose owning task is no longer active, has existed longer than the configured maximum workspace age, THE Workspace_Manager SHALL destroy the orphaned Workspace and SHALL emit a `workspace.destroyed` event carrying the workspace identifier and the orphan-expiry reason.

### Requirement 7: Verification Pipeline

**User Story:** As a developer, I want generated code verified before it is committed, so that only passing changes reach my repository.

#### Acceptance Criteria

1. WHEN a task produces output, THE Verification_Pipeline SHALL run every enabled advisory verifier stage concurrently, each bounded by its configured per-stage timeout.
2. IF an advisory verifier stage fails or exceeds its configured timeout, THEN THE Verification_Pipeline SHALL record that stage's status and SHALL continue running the remaining advisory stages without halting.
3. WHEN advisory verifier stages complete, THE Verification_Pipeline SHALL merge their results into a result map keyed by stage name that is independent of stage completion order.
4. WHERE no advisory verifier stage is enabled, THE Verification_Pipeline SHALL produce an empty advisory result map and proceed to the blocking gate stages.
5. WHEN the advisory stages have been merged, THE Verification_Pipeline SHALL run the blocking gate stages in their declared order.
6. IF a blocking gate stage fails, THEN THE Verification_Pipeline SHALL halt the pipeline before the commit step, SHALL retain the task's Workspace, and SHALL request a decision from the Policy_Engine identifying the failing stage.
7. WHEN any verifier stage runs, THE Verification_Pipeline SHALL emit a `verify.stage` event carrying the task identifier, stage name, a status of passed, failed, or timed-out, and detail.
8. WHEN all blocking gate stages pass, THE Verification_Pipeline SHALL emit a `verify.passed` event for the task.

### Requirement 8: Failure Policy Decisions

**User Story:** As a developer, I want consistent, recorded decisions when a task fails, so that I understand whether Forge retried, escalated, or skipped.

#### Acceptance Criteria

1. WHEN a blocking stage fails, THE Policy_Engine SHALL deterministically decide exactly one of retry, escalate, or skip based on the configured rules and the task's attempt count.
2. WHEN the Policy_Engine makes a decision, THE Policy_Engine SHALL emit exactly one `policy.decision` event carrying the subject, decision, rule, and inputs including the task's attempt count.
3. WHEN the Policy_Engine makes a decision, THE Policy_Engine SHALL write a Decision_Record carrying the subject, decision, rule, and inputs.
4. WHEN the count of tool failures for a task reaches the configured escalation threshold, THE Policy_Engine SHALL escalate the task to the alternate coding tool.
5. IF escalation is selected but no alternate coding tool is present in the Capability_Registry, THEN THE Policy_Engine SHALL mark the task as failed and SHALL emit a decision indicating that no alternate coding tool is available.
6. WHEN the retry budget for a task is exhausted, THE Policy_Engine SHALL mark the task as failed and SHALL allow the workflow to continue executing tasks that do not depend on the failed task while not executing tasks that depend on it.
7. WHEN a task fails, THE Workflow_Engine SHALL emit exactly one `task.fail` event carrying the task identifier, failing stage, reason, and the policy decision.

### Requirement 9: Commit and Finalization

**User Story:** As a developer, I want verified task changes committed and pushed to my repository, so that the work persists in version control.

#### Acceptance Criteria

1. WHEN a `verify.passed` event is emitted for a task, THE Workflow_Engine SHALL commit that task's changes from its Workspace into the canonical repository and SHALL emit a `commit.done` event carrying the commit SHA and the list of changed file paths.
2. THE Workflow_Engine SHALL commit a task's changes to the canonical repository only after a `verify.passed` event has been emitted for that task, and SHALL NOT commit changes for any task that has not emitted a `verify.passed` event.
3. WHEN all tasks have been processed, THE Workflow_Engine SHALL push the canonical repository to the VCS and SHALL emit a `build.done` event carrying a build summary, the list of committed task identifiers, and the list of skipped task identifiers.
4. WHERE an action is configured as approval-gated, WHEN that action is reached, THE Workflow_Engine SHALL pause before performing the action, SHALL request explicit user approval, and SHALL emit an event indicating that approval is pending for the action.
5. IF a commit operation for a task fails, THEN THE Workflow_Engine SHALL leave the canonical repository unchanged for that task, SHALL NOT advance the task to a committed state, and SHALL emit an error event indicating the commit failure and the affected task identifier.
6. IF the push to the VCS fails, THEN THE Workflow_Engine SHALL retain the committed changes in the canonical repository, SHALL NOT emit a `build.done` event, and SHALL emit an error event indicating the push failure.
7. IF the user rejects an approval-gated action or does not respond within the configured approval timeout, THEN THE Workflow_Engine SHALL NOT perform the action and SHALL emit an event indicating that the action was not approved.

### Requirement 10: Documentation Maintenance

**User Story:** As a developer, I want documentation kept in sync with code, so that docs do not silently drift from reality.

#### Acceptance Criteria

1. WHEN a `commit.done` event is emitted, THE Workflow_Engine SHALL compute a twin diff describing modules added, changed, or removed relative to the prior Digital_Twin state.
2. WHEN a twin diff is computed, THE Doc_Writer SHALL update the README and the `docs` files to reflect the modules added, changed, or removed and SHALL emit a `doc_updated` event carrying the changed files and a twin-diff summary.
3. IF the Doc_Writer cannot update a documentation file, THEN THE Forge_Runtime SHALL leave that file's content unchanged, SHALL record a documentation drift entry for that file, and SHALL emit an event carrying the failure reason.
4. AFTER finalization, FOR each module changed in the twin diff, THE Forge_Runtime SHALL either update the corresponding documentation or record a documentation drift entry, and SHALL emit an event carrying the recorded drift entries.
5. WHERE the build mode is `document`, THE Workflow_Engine SHALL read every documentation file referenced in the Documentation_State, diff it against the Digital_Twin, and update each documentation file whose content does not match the repository.
6. WHERE the build mode is `document`, IF a documentation file already matches the Digital_Twin, THEN THE Workflow_Engine SHALL leave that file unchanged and SHALL NOT record a drift entry for it.

### Requirement 11: Role-Based Model Routing with Fallback

**User Story:** As a developer, I want model selection to follow a defined, health-aware fallback chain, so that builds continue when a provider is down.

#### Acceptance Criteria

1. WHEN a Role completion is requested, THE Model_Router SHALL attempt providers strictly in the order defined by the configured chain for that Role.
2. IF a provider is not present in the Capability_Registry or its Circuit_Breaker is open, THEN THE Model_Router SHALL skip that provider without attempting a call.
3. WHEN a provider call returns successfully, THE Model_Router SHALL return that completion and SHALL record the provider's success on its Circuit_Breaker.
4. IF a provider call fails with a transient error, being a timeout, rate-limit, or temporary-unavailability failure, THEN THE Model_Router SHALL apply the configured cancellable backoff bounded between 1 and 30 seconds before the next attempt, SHALL retry that same provider up to the configured maximum of 3 attempts, and SHALL record each failure on the Circuit_Breaker.
5. IF a provider call fails with a permanent error, being an authentication, authorization, malformed-request, or unsupported-model failure, THEN THE Model_Router SHALL stop retrying that provider, SHALL record the failure on the Circuit_Breaker, and SHALL advance to the next provider in the chain.
6. IF every provider in the chain is skipped or exhausted, THEN THE Model_Router SHALL raise a model-unavailable error identifying the Role and the list of providers attempted, SHALL NOT return a partial or substitute completion, and SHALL record the attempt log in the Audit_Trail.
7. WHILE the capability state is held fixed, THE Model_Router SHALL select the same provider and model sequence for repeated requests of the same Role.
8. WHEN the Model_Router selects or falls back between providers, THE Model_Router SHALL emit a `model.selected` or `model.fallback` event carrying the Role, provider, model, attempt, and reason.
9. IF a provider call does not return within the configured per-call timeout of 60 seconds, THEN THE Model_Router SHALL cancel the in-flight call, SHALL treat it as a transient error, and SHALL record the failure on the Circuit_Breaker.

### Requirement 12: Cost and Budget Governance

**User Story:** As a developer, I want a per-session spending limit enforced, so that a build cannot run away with token costs.

#### Acceptance Criteria

1. WHEN a session is created, THE Forge_Runtime SHALL initialize that session's Session_Budget to the configured per-session token limit and SHALL set its consumed amount to zero.
2. IF the estimated token count for a request exceeds the remaining Session_Budget, THEN THE Model_Router SHALL raise a budget-exceeded error identifying the requested Role and the remaining Session_Budget, SHALL NOT issue the model call, and SHALL leave the Session_Budget consumed amount unchanged.
3. WHEN the Model_Router raises a budget-exceeded error, THE Model_Router SHALL emit a budget-exceeded event carrying the Role, the estimated token count, and the remaining Session_Budget, and SHALL record the attempt in the Audit_Trail.
4. WHEN a model call completes, THE Model_Router SHALL add the actual token usage of that call to the Session_Budget consumed amount before the next request is evaluated.
5. WHEN a developer queries runtime status, THE Runtime_Inspector SHALL report the configured Session_Budget limit, the consumed amount, and the remaining amount, and SHALL NOT invoke an AI model.

### Requirement 13: Capability Discovery at Startup

**User Story:** As an operator, I want Forge to probe all configured resources at startup, so that the runtime only relies on capabilities that are actually available.

#### Acceptance Criteria

1. WHEN the runtime starts, THE Discovery_Procedure SHALL load and validate the configuration files before probing any resource.
2. IF configuration validation detects a schema error, THEN THE Discovery_Procedure SHALL halt startup within 1 second, SHALL NOT register any Capability or enter a partial-boot state, and SHALL emit an error identifying the configuration file and the validation failure.
3. WHEN configuration is validated, THE Discovery_Procedure SHALL probe all configured resources concurrently using a per-probe timeout of 5 seconds.
4. WHEN a resource health check returns healthy within the per-probe timeout, THE Discovery_Procedure SHALL register the resource as a Capability in the Capability_Registry and SHALL emit a `capability.registered` event carrying the resource identifier.
5. IF a resource health check returns unhealthy or exceeds the per-probe timeout, THEN THE Discovery_Procedure SHALL NOT register the resource and SHALL emit a `capability.degraded` event carrying the resource identifier and the reason.
6. WHEN every configured resource has either completed its health check or reached its per-probe timeout, THE Discovery_Procedure SHALL emit a `forge.boot.discovery_complete` event carrying the set of healthy and unhealthy resources.
7. WHEN the `forge.boot.discovery_complete` event has been emitted, THE Forge_Runtime SHALL emit a `forge.ready` event carrying the operational mode, Capability_Summary, and startup report.

### Requirement 14: Continuous Health Monitoring

**User Story:** As an operator, I want capabilities re-checked while the runtime runs, so that a provider going down is detected and surfaced.

#### Acceptance Criteria

1. WHILE the runtime is running, THE Health_Monitor SHALL re-probe every registered Capability on the configured monitoring interval, bounded between 5 and 300 seconds and defaulting to 30 seconds, using a per-probe timeout bounded between 1 and 60 seconds and defaulting to 10 seconds.
2. IF a single probe of a registered Capability fails or exceeds the per-probe timeout but the configured consecutive failure threshold has not been reached, THEN THE Health_Monitor SHALL keep the Capability registered and SHALL increment its consecutive failure count.
3. WHEN a registered Capability reaches the configured consecutive failure threshold, bounded between 1 and 10 and defaulting to 3, THE Health_Monitor SHALL deregister the Capability, emit a `capability.degraded` event carrying the Capability name and failure reason, and record a capability-transition Decision_Record.
4. WHEN a previously unhealthy resource returns healthy for the configured recovery threshold of consecutive probes, bounded between 1 and 10 and defaulting to 2, THE Health_Monitor SHALL re-register the Capability and emit a `capability.recovered` event carrying the Capability name.
5. THE Capability_Registry SHALL be the only writable shared capability state, written only by the Discovery_Procedure at boot and the Health_Monitor continuously.

### Requirement 15: Registry-Mediated Capability Resolution

**User Story:** As a maintainer, I want all components to resolve capabilities through the registry, so that the system stays modular and replaceable.

#### Acceptance Criteria

1. THE Forge_Runtime SHALL resolve every Capability by name or by Role through the Capability_Registry, and SHALL NOT read raw configuration or construct an Adapter_Layer adapter directly to obtain a Capability.
2. IF a requested Capability is not present in the Capability_Registry at resolution time, THEN THE Capability_Registry SHALL raise a capability-unavailable error identifying the requested capability name or Role and SHALL NOT return any adapter to the requesting component.
3. WHEN a new adapter file implementing a plugin protocol is added and named in configuration, THE Discovery_Procedure SHALL discover and health-check that adapter at the next runtime start using the configured per-probe timeout.
4. WHEN a newly discovered adapter's health check returns healthy, THE Capability_Registry SHALL register it as a Capability and the Forge_Runtime SHALL resolve it through the Capability_Registry without any change to existing orchestration code.
5. IF a newly discovered adapter's health check returns unhealthy or exceeds the configured per-probe timeout, THEN THE Discovery_Procedure SHALL NOT register the adapter as a Capability and SHALL emit a `capability.degraded` event carrying the reason.

### Requirement 16: Minimum Capability and Degraded Mode

**User Story:** As a developer, I want Forge to start honestly even when capabilities are missing, so that I know exactly what is unavailable instead of facing a silent failure.

#### Acceptance Criteria

1. IF no provider can serve the Coder role, or no VCS connector is available, or no coding tool is available, THEN THE Forge_Runtime SHALL enter DEGRADED mode and SHALL record each missing required capability name and reason in the Capability_Summary.
2. WHILE the Forge_Runtime is in DEGRADED mode, IF a build request is received, THEN THE Application_Layer SHALL reject the request without starting a build and SHALL return an error response indicating DEGRADED mode and the precise list of missing capabilities and reasons.
3. WHILE the Forge_Runtime is in DEGRADED mode, THE Application_Layer SHALL continue to serve chat, status, capability, health, and explain requests.
4. WHEN the minimum required capabilities are met but a soft capability such as the Reviewer role or the vector store is unavailable, THE Forge_Runtime SHALL enter OPERATIONAL mode, record the soft degradation as a Decision_Record, and record it as an entry in the Capability_Summary.
5. WHEN the Operational_Mode changes, THE Forge_Runtime SHALL emit a capability transition event and SHALL record a capability-transition Decision_Record.
6. WHILE the Forge_Runtime is OPERATIONAL, IF a required capability becomes unavailable, THEN THE Forge_Runtime SHALL transition to DEGRADED mode and SHALL record the missing-capability reasons in the Capability_Summary.

### Requirement 17: Typed Event Contract and Ordering

**User Story:** As a maintainer, I want every runtime occurrence to be a typed, ordered, causally linked event, so that the stream is replayable and explainable.

#### Acceptance Criteria

1. WHEN any runtime occurrence happens, THE Forge_Runtime SHALL publish a typed Event carrying a non-empty `type`, a `payload`, a non-empty `correlation_id`, a non-empty `source`, a globally unique `event_id`, and a `timestamp` expressed in UTC with at least millisecond precision.
2. IF a runtime occurrence cannot be published with all of `type`, `payload`, `correlation_id`, `source`, `event_id`, and `timestamp` populated, THEN THE Forge_Runtime SHALL reject the occurrence, retain no partial Event on the bus, and emit an error indication identifying the missing fields.
3. WHEN an Event is published, THE Event_Bus SHALL assign a `seq` that is unique and strictly increasing by exactly 1 among events sharing the same `correlation_id`, starting at 1 for the first such Event.
4. WHEN an Event is caused by a prior Event, THE Forge_Runtime SHALL set the new Event's `causation_id` to the `event_id` of an Event with a smaller `seq` in the same session.
5. IF an Event has no causing prior Event in the same session, THEN THE Forge_Runtime SHALL leave the `causation_id` unset.
6. THE Event_Bus SHALL deliver every Event to each subscriber at least once and in ascending `seq` order per session.
7. IF delivery of an Event to a subscriber fails, THEN THE Event_Bus SHALL retry delivery up to a maximum of 5 attempts before marking the delivery as failed and emitting an error indication identifying the affected subscriber, session identifier, and `seq`.
8. THE Forge_Runtime SHALL make every subscriber idempotent on the pair of session identifier and `seq`, such that processing a duplicate pair produces no additional observable side effect beyond the first delivery.

### Requirement 18: Streaming Backpressure

**User Story:** As a developer, I want event streaming to remain stable under a slow client, so that the runtime does not exhaust memory.

#### Acceptance Criteria

1. THE Event_Bus SHALL maintain a separate queue for each subscriber bounded to a configurable maximum depth, defaulting to 1,000 events, beyond which no additional events are buffered for that subscriber.
2. IF enqueuing an event would cause a subscriber queue to exceed its maximum depth AND the queue contains one or more coalescible `token` events, THEN THE Event_Bus SHALL drop the oldest coalescible `token` events from that queue until the new event fits within the maximum depth.
3. THE Event_Bus SHALL never drop lifecycle events, including all `*.start`, `*.done`, `*.fail`, `task.done`, `build.done`, `question`, and `error` events.
4. IF enqueuing a lifecycle event would cause a subscriber queue to exceed its maximum depth AND the queue contains no coalescible `token` events to drop, THEN THE Event_Bus SHALL block the producer for at most a configurable timeout, defaulting to 5 seconds, and on timeout SHALL persist the lifecycle event to durable storage rather than dropping it.
5. WHILE a subscriber queue is at its maximum depth, THE Event_Bus SHALL keep that subscriber's total buffered events at or below the configured maximum depth so that per-subscriber memory remains bounded.

### Requirement 19: Explainable Audit Trail

**User Story:** As a developer, I want every non-trivial decision recorded with a reproducible rationale, so that I can ask why Forge did something and get a truthful answer.

#### Acceptance Criteria

1. WHEN the Forge_Runtime makes a decision whose kind is a member of the defined set of decision kinds, THE Forge_Runtime SHALL write a Decision_Record to the Audit_Trail carrying the kind, subject, inputs, decision, rationale, alternatives, and the causing event's `event_id`.
2. WHEN a Decision_Record rationale is constructed, THE Forge_Runtime SHALL derive it solely from the record's own inputs such that identical inputs recompute to an identical rationale, and SHALL NOT derive it from AI-generated prose.
3. THE Audit_Trail SHALL persist the event stream as an ordered projection with a unique key on session identifier and `seq`, with `seq` strictly increasing per session.
4. WHEN events are replayed in `seq` order, THE Forge_Runtime SHALL reconstruct a ForgeState whose persisted fields are field-for-field equal to the original ForgeState at the corresponding `seq`.
5. IF a Decision_Record write fails, THEN THE Forge_Runtime SHALL retain the originating event, SHALL surface an audit-write error, and SHALL NOT discard the decision.
6. IF an event with an already-persisted pair of session identifier and `seq` is presented to the Audit_Trail, THEN THE Audit_Trail SHALL keep the existing record and SHALL NOT create a duplicate.

### Requirement 20: Runtime Inspector Without Inference

**User Story:** As a developer, I want to query what Forge is doing and why, so that I get deterministic answers grounded in stored state rather than guesses.

#### Acceptance Criteria

1. WHEN a developer requests the current workflow state, THE Runtime_Inspector SHALL return the current node, worker status, task queue, and active task derived from runtime state, and SHALL respond within 2 seconds.
2. IF no build is in progress for the session when the current workflow state is requested, THEN THE Runtime_Inspector SHALL return the current node, an empty active task, and an empty task queue rather than an error.
3. WHEN a developer requests an explanation of the last decision, THE Runtime_Inspector SHALL return the most recent Decision_Record for the session, including its kind, subject, inputs, decision, rationale, and alternatives.
4. IF no Decision_Record exists for the session when an explanation of the last decision is requested, THEN THE Runtime_Inspector SHALL return a not-found response indicating that no decision has been recorded for the session.
5. WHEN a developer requests an explanation of a task by task identifier, THE Runtime_Inspector SHALL reconstruct that task's history including model selections, verifier outcomes, retries, and resulting commit by following causation links in the Audit_Trail.
6. IF the requested task identifier does not exist within the session when a task explanation is requested, THEN THE Runtime_Inspector SHALL return a not-found response carrying the requested task identifier.
7. THE Runtime_Inspector SHALL derive every response solely from runtime state, the Audit_Trail, and the Capability_Registry, and SHALL NOT invoke an AI model.

### Requirement 21: Crash Recovery and Resume

**User Story:** As a developer, I want an interrupted build to resume after a restart, so that work is not lost when the process crashes.

#### Acceptance Criteria

1. WHEN a workflow node completes, THE Workflow_Engine SHALL checkpoint to the relational store the redacted ForgeState, the completed node identifier, and the highest emitted `seq`, before the next node begins.
2. IF a checkpoint write fails, THEN THE Workflow_Engine SHALL halt at the completed node, SHALL emit an error event, and SHALL NOT advance to the next node.
3. WHEN the runtime restarts, THE Workflow_Engine SHALL identify every session whose latest checkpoint references a non-terminal node and SHALL resume each from its last completed node using the persisted ForgeState values before accepting new messages for that session.
4. IF a task had not emitted a `commit.done` event at the time of the crash, THEN THE Workflow_Engine SHALL destroy its Workspace, emit a `workspace.destroyed` event, and re-queue the task within its existing retry budget.
5. WHEN a client reconnects to a session stream, THE Application_Layer SHALL replay, in strictly increasing `seq` order, the events missed since the client's last received `seq`.
6. IF the client's last received `seq` precedes the oldest retained event for the session, THEN THE Application_Layer SHALL send the current state snapshot followed by the retained events in `seq` order.

### Requirement 22: Secret Handling and Redaction

**User Story:** As a security-conscious developer, I want my tokens and keys never persisted in plaintext, so that secrets cannot leak through state, logs, or audit records.

#### Acceptance Criteria

1. THE Forge_Runtime SHALL store the VCS token and provider keys in plaintext only in the per-session Secret_Holder and the process environment, and SHALL NOT write any raw secret value to disk, the relational store, the Audit_Trail, or any log.
2. WHEN any state snapshot, audit record, event payload, prompt artifact, or response artifact is persisted, THE Forge_Runtime SHALL replace each VCS token and provider key value with a non-reversible masked placeholder or token reference before the write occurs, such that no raw secret value appears in the persisted output.
3. THE Forge_Runtime SHALL store a token reference rather than the token value in persisted state snapshots.
4. THE Forge_Runtime SHALL execute model-generated code in sandboxed Workspaces that contain no VCS token, provider key, or other ambient credential.
5. WHEN a commit or push operation requires credentials, THE Forge_Runtime SHALL perform that operation from the runtime using the Secret_Holder rather than from any sandboxed Workspace.
6. WHEN a session terminates, THE Forge_Runtime SHALL clear the VCS token and provider keys from the per-session Secret_Holder so that no plaintext secret value is retained after session end.
7. IF redaction of a VCS token or provider key cannot be applied before a persistence or emission operation, THEN THE Forge_Runtime SHALL abort that operation, retain no raw secret value in the target, and record an error indicating that redaction failed.

### Requirement 23: Layered Architecture Boundaries

**User Story:** As a maintainer, I want strict layer boundaries, so that the system stays modular and any adapter is replaceable.

#### Acceptance Criteria

1. WHEN the layer-boundary check executes, THE Forge_Runtime SHALL verify that every module import targets either its own layer or exactly one adjacent layer, such that no import crosses more than one layer boundary.
2. THE Presentation_Layer SHALL depend only on the runtime API and the typed event catalog, and SHALL contain zero model-selection, tool-invocation, or orchestration logic.
3. THE Application_Layer SHALL translate exclusively between transport messages and runtime API calls or events, and SHALL contain zero engineering logic, where engineering logic means model-selection, tool-invocation, verification, or workflow-orchestration decisions.
4. THE Forge_Runtime SHALL depend only on plugin protocol interfaces and SHALL contain zero HTTP-specific or transport-specific logic.
5. THE Adapter_Layer SHALL translate exactly one protocol call into one infrastructure call and SHALL contain zero business logic and zero orchestration logic.
6. IF a module import crosses more than one layer boundary or targets a non-adjacent layer, THEN THE Forge_Runtime SHALL fail the boundary check with an error identifying the offending import path and SHALL leave all existing layer modules unchanged.

### Requirement 24: Interrupt Handling

**User Story:** As a developer, I want to pause, resume, redirect, or stop a build at any time, so that I stay in control of the autonomous runtime.

#### Acceptance Criteria

1. WHILE a build is in progress, WHEN an interrupt is requested, THE Workflow_Engine SHALL pause execution within 2 seconds of receiving the request and SHALL retain all in-progress build state for later resumption.
2. WHEN execution is paused following an interrupt request, THE Workflow_Engine SHALL surface the interrupt message to the developer within 2 seconds of pausing.
3. WHEN a developer requests resume after an interrupt, THE Workflow_Engine SHALL continue execution from the paused point using the retained build state and SHALL emit an `interrupt.resumed` event within 2 seconds.
4. IF a developer requests resume while no interrupt is active, THEN THE Workflow_Engine SHALL reject the request, SHALL leave execution state unchanged, and SHALL return an error indication that no paused build exists.
5. WHEN a developer requests redirect during an interrupt with a non-empty new direction, THE Workflow_Engine SHALL return to planning seeded with the new direction.
6. IF a developer requests redirect during an interrupt with an empty or whitespace-only direction, THEN THE Workflow_Engine SHALL reject the redirect, SHALL keep the build in the paused state, and SHALL return an error indication that a non-empty direction is required.
7. WHEN a developer issues a stop or interrupt during an in-flight model backoff, THE Model_Router SHALL cancel the in-flight backoff within 2 seconds and SHALL NOT issue further retry attempts for that backoff.

### Requirement 25: Outcome Recording

**User Story:** As a developer, I want build outcomes recorded, so that recommendations can be surfaced without being applied automatically.

#### Acceptance Criteria

1. WHEN a build finalizes, THE Learning_Recorder SHALL record one outcome entry per executed task, each entry including task type, tool, model, role, outcome status, retry count as a non-negative integer, and escalation flag as a boolean, and SHALL persist each entry to the relational store.
2. THE Learning_Recorder SHALL record the outcome status of each entry as exactly one of the values success or failure.
3. IF persisting an outcome entry fails, THEN THE Learning_Recorder SHALL complete the build without discarding the build result and SHALL emit an event indicating the recording failure together with the affected task identifier.
4. WHEN a developer requests recommendations for a session, THE Learning_Recorder SHALL return the recommendations derived from the recorded outcome entries for that session.
5. THE Learning_Recorder SHALL NOT apply any recommendation automatically.

### Requirement 26: API and Streaming Endpoints

**User Story:** As a developer, I want REST and WebSocket endpoints to drive and observe a session, so that any client can interact with the runtime identically.

#### Acceptance Criteria

1. THE Application_Layer SHALL expose session-management, messaging, artifact-retrieval, control, and runtime-inspection endpoints over REST, and SHALL expose exactly one WebSocket event stream per session.
2. WHEN a developer connects to a session stream, THE Application_Layer SHALL forward each typed Event for that session to the client in strictly increasing `seq` order, preserving the original Event payload, and without adding engineering logic.
3. IF a developer connects to a stream for a session identifier that does not exist, THEN THE Application_Layer SHALL reject the connection with an error indicating the session was not found and SHALL NOT open a stream.
4. WHEN a developer requests an explain or runtime endpoint, THE Application_Layer SHALL serve the response solely from the Runtime_Inspector and SHALL NOT invoke an AI model.
5. THE Application_Layer SHALL require valid authentication credentials on all session, messaging, artifact, control, and runtime endpoints and on the session stream.
6. IF a request to a session, messaging, artifact, control, or runtime endpoint, or a session-stream connection, omits or carries invalid authentication credentials, THEN THE Application_Layer SHALL reject the request with an authentication-failure response and SHALL NOT perform the requested operation.
