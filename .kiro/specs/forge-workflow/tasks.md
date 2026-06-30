# Implementation Plan: Forge LangGraph Workflow Integration

## Overview

Wire all existing Forge runtime components into a single LangGraph state machine. The implementation language is Python (matching the existing codebase). All runtime components already exist with protocol-based DI — this plan concerns only the workflow wiring layer: state definition, node functions, graph construction, bootstrap, and application entry point.

## Task Dependency Graph

```json
{
  "waves": [
    { "tasks": [1], "description": "Foundation: ForgeState + RuntimeDeps" },
    { "tasks": [2, 3, 4], "description": "Node functions (can be parallel)" },
    { "tasks": [5], "description": "Graph builder + routing" },
    { "tasks": [6], "description": "Bootstrap sequence" },
    { "tasks": [7], "description": "Application entry point" },
    { "tasks": [8], "description": "Integration tests" },
    { "tasks": [9], "description": "Final checkpoint" }
  ]
}
```

## Tasks

- [x] 1. Extend ForgeState and define RuntimeDeps
  - [x] 1.1 Extend ForgeState TypedDict in `app/runtime/models.py`
    - Add new fields: `intent`, `message`, `node_path`, `task_ordering`, `current_task_index`, `spec_artifact_uri`, `commit_shas`, `doc_updates`, `needs_clarification`, `all_tasks_done`, `approval_pending`
    - Keep `total=False` semantics
    - _Requirements: 1.1, 1.3_

  - [x] 1.2 Create `app/workflow/__init__.py` and `app/workflow/deps.py` with RuntimeDeps dataclass
    - Define RuntimeDeps holding all component references (EventBus, Registry, ModelRouter, SessionManager, Classifier, Clarification, Specification, Planner, WorkspaceManager, TaskDispatcher, VerificationPipeline factory, PolicyEngine, CommitWorkflow, Finalization, Documentation, Inspector, CrashRecovery, InterruptHandler, LearningRecorder, HealthMonitor, Discovery)
    - _Requirements: 14.1, 16.1_

  - [ ]* 1.3 Write property test for state merge semantics
    - **Property 1: State merge preserves untouched keys**
    - **Validates: Requirements 1.2**

- [x] 2. Implement node functions: intake, classify, clarify
  - [x] 2.1 Create `app/workflow/nodes/__init__.py` and `app/workflow/nodes/intake.py`
    - Implement `make_intake_node(deps) -> NodeFn` factory
    - Validate message non-empty, set status="processing", append "intake" to node_path
    - Write checkpoint via deps.recovery
    - Handle invalid input: append error with code "invalid_input", set status="failed"
    - _Requirements: 3.1, 3.2, 3.3, 2.1, 2.2, 2.3_

  - [x] 2.2 Create `app/workflow/nodes/classify.py`
    - Implement `make_classify_node(deps) -> NodeFn` factory
    - Delegate to deps.classifier, set intent field
    - Ensure deterministic: no AI call for status_query/interrupt intents
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 2.3 Create `app/workflow/nodes/clarify.py`
    - Implement `make_clarify_node(deps) -> NodeFn` factory
    - Check specification completeness via deps.clarification
    - Set needs_clarification flag, emit question events when inputs missing
    - Record answers in session context
    - _Requirements: 5.1, 5.2, 5.3_

  - [ ]* 2.4 Write property tests for intake and classify nodes
    - **Property 4: Intake validation — valid inputs produce processing state**
    - **Property 5: Intake validation — invalid inputs produce error state**
    - **Property 6: Classification determinism**
    - **Validates: Requirements 3.1, 3.2, 4.1, 4.2**

- [x] 3. Implement node functions: architect, plan, execute
  - [x] 3.1 Create `app/workflow/nodes/architect.py`
    - Implement `make_architect_node(deps) -> NodeFn` factory
    - Invoke specification component via model router (Role.ARCHITECT)
    - Store artifact URI, populate tasks list, emit spec.ready and tasks.ready
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 3.2 Create `app/workflow/nodes/plan.py`
    - Implement `make_plan_node(deps) -> NodeFn` factory
    - Delegate to deps.planner for topological sort
    - Set task_ordering, current_task_index=0
    - Handle cycle detection: set status="plan_failed", populate errors
    - _Requirements: 7.1, 7.2, 7.3_

  - [x] 3.3 Create `app/workflow/nodes/execute.py`
    - Implement `make_execute_node(deps) -> NodeFn` factory
    - Create workspace via deps.workspace_manager
    - Dispatch current task via deps.task_dispatcher
    - Update task status to "verifying", emit workspace.created
    - _Requirements: 8.1, 8.2, 8.3_

  - [ ]* 3.4 Write property test for plan node topological ordering
    - **Property 8: Topological ordering validity**
    - **Validates: Requirements 7.1**

- [x] 4. Implement node functions: verify, commit, doc_update, finalize, status, interrupt, policy
  - [x] 4.1 Create `app/workflow/nodes/verify.py`
    - Implement `make_verify_node(deps) -> NodeFn` factory
    - Construct pipeline via deps.verification_pipeline_factory
    - Run stages in order, record results, emit verify.passed or record failure
    - _Requirements: 9.1, 9.2, 9.3_

  - [x] 4.2 Create `app/workflow/nodes/commit.py`
    - Implement `make_commit_node(deps) -> NodeFn` factory
    - Commit via VCS connector from registry, append SHA to commit_shas
    - Increment current_task_index, set all_tasks_done when complete
    - Emit commit.done event
    - _Requirements: 10.1, 10.2, 10.3_

  - [x] 4.3 Create `app/workflow/nodes/doc_update.py` and `app/workflow/nodes/finalize.py`
    - doc_update: invoke DocWriter, populate doc_updates, handle graceful degradation
    - finalize: push via VCS, emit build.done, record learning, set status="completed"
    - _Requirements: 11.1, 11.2, 11.3, 12.1, 12.2, 12.3_

  - [x] 4.4 Create `app/workflow/nodes/status.py`, `app/workflow/nodes/interrupt.py`, `app/workflow/nodes/policy.py`
    - status: delegate to deps.inspector, return status summary
    - interrupt: delegate to deps.interrupt_handler
    - policy: delegate to deps.policy_engine, record decision, determine retry/skip/escalate
    - _Requirements: 13.2, 13.3, 13.5_

  - [ ]* 4.5 Write property tests for verify and commit nodes
    - **Property 9: Commit index progression**
    - **Property 10: Verification result integrity**
    - **Validates: Requirements 9.1, 9.2, 10.1, 10.2**

- [x] 5. Graph builder and routing functions
  - [x] 5.1 Create `app/workflow/routing.py`
    - Implement `route_after_classify(state) -> str`
    - Implement `route_after_verify(state) -> str`
    - Implement `route_after_policy(state) -> str`
    - Implement `route_after_commit(state) -> str`
    - All are pure functions on ForgeState
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_

  - [x] 5.2 Create `app/workflow/graph.py`
    - Implement `build_forge_graph(deps: RuntimeDeps) -> CompiledStateGraph`
    - Register all 13 nodes, set entry point to "intake"
    - Wire linear edges: intake→classify, clarify→architect, architect→plan, plan→execute, doc_update→finalize, finalize→END, status→END, interrupt→END
    - Wire conditional edges: classify→routing, verify→routing, policy→routing, commit→routing, execute→verify (always)
    - _Requirements: 14.1, 14.2, 14.3, 14.4_

  - [ ]* 5.3 Write property tests for routing functions
    - **Property 7: Routing function correctness**
    - **Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7**

- [x] 6. Bootstrap sequence
  - [x] 6.1 Create `app/workflow/bootstrap.py`
    - Implement `bootstrap(deps: RuntimeDeps) -> None`
    - Load/validate configuration
    - Run concurrent discovery via deps.discovery
    - Register healthy capabilities in deps.registry
    - Start deps.health_monitor background task
    - Evaluate operational mode from registry summary
    - Emit `forge.ready` event via deps.event_bus
    - _Requirements: 15.1, 15.2, 15.3, 15.4_

  - [x] 6.2 Implement `assemble_deps() -> RuntimeDeps` helper
    - Instantiate all runtime components with proper dependency injection
    - Return fully wired RuntimeDeps container
    - _Requirements: 16.1_

  - [ ]* 6.3 Write unit tests for bootstrap error handling
    - Test config validation failure halts startup (Req 15.5)
    - Test forge.ready event contains mode and summary
    - _Requirements: 15.4, 15.5_

- [x] 7. Application entry point
  - [x] 7.1 Create `app/workflow/app.py`
    - Implement `create_app() -> FastAPI`
    - Register lifespan: on startup call assemble_deps + bootstrap, on shutdown stop health_monitor
    - Store compiled graph in app state
    - _Requirements: 16.1, 16.2, 16.4_

  - [x] 7.2 Add API route for workflow invocation
    - POST endpoint accepting message + session_id
    - Constructs initial ForgeState, calls `graph.ainvoke(state)`
    - Returns final state summary (status, commit_shas, errors)
    - _Requirements: 16.3_

  - [x] 7.3 Create `main.py` at project root (or update existing)
    - Import and run: `uvicorn app.workflow.app:app`
    - Document CLI usage in docstring
    - _Requirements: 16.4_

- [ ] 8. Integration tests
  - [ ]* 8.1 Write integration test: full happy-path flow
    - Mock all external adapters (AI provider, VCS, artifact store)
    - Invoke graph with a build_intent message
    - Assert state progresses: intake → classify → clarify → architect → plan → execute → verify → commit → doc_update → finalize
    - Verify final status == "completed", commit_shas non-empty
    - _Requirements: 14.4, 3.1, 4.1, 7.1, 10.1, 12.2_

  - [ ]* 8.2 Write integration test: status_query and interrupt short-circuits
    - Invoke graph with status_query message, assert it reaches status node and ends
    - Invoke graph with interrupt message, assert it reaches interrupt node and ends
    - _Requirements: 13.2, 13.3_

  - [ ]* 8.3 Write integration test: verification failure triggers policy retry loop
    - Mock verification to fail on first attempt, pass on second
    - Assert graph routes: execute → verify → policy → execute → verify → commit
    - Verify retry decision recorded in decisions list
    - _Requirements: 9.2, 13.5, 10.1_

  - [ ]* 8.4 Write integration test: bootstrap failure on invalid config
    - Provide invalid configuration
    - Assert bootstrap raises/halts before forge.ready
    - Assert error event emitted
    - _Requirements: 15.5_

- [x] 9. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.
  - Run `pytest tests/ --run` and verify no failures
  - Verify ruff linting passes with `ruff check app/workflow/`

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each node function follows the factory pattern: `make_X_node(deps) -> async fn(state) -> dict`
- All runtime components already exist — nodes are thin wrappers delegating to them
- Property tests use hypothesis (already in dev dependencies)
- Integration tests use pytest-asyncio (already configured)
