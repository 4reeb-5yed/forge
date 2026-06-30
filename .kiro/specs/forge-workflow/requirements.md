# Requirements Document

## Introduction

This document defines the requirements for integrating all existing Forge runtime components into a single LangGraph state machine. The integration wires node functions (delegating to existing components) into a directed graph with conditional routing, driven by the `ForgeState` typed dict. The feature also defines the bootstrap sequence that brings the runtime to operational readiness, and the FastAPI application entry point.

## Glossary

- **ForgeState**: The LangGraph TypedDict carrying all workflow state between nodes.
- **RuntimeDeps**: A dataclass container holding all instantiated runtime component references, assembled at startup.
- **Node_Function**: An async function receiving ForgeState and returning a partial dict of state updates.
- **Graph_Builder**: The function that constructs and compiles the LangGraph StateGraph.
- **Bootstrap**: The startup sequence that discovers, registers, and evaluates capabilities before the workflow is invokable.
- **Routing_Function**: A pure function inspecting ForgeState to determine the next graph node.
- **EventBus**: The publish-subscribe system for workflow lifecycle events.
- **Registry**: The capability registry holding healthy provider entries.
- **ModelRouter**: Resolves abstract Roles to concrete AI providers via the Registry.

## Requirements

### Requirement 1: ForgeState Definition

**User Story:** As a workflow developer, I want a well-defined typed state object, so that all nodes share a consistent interface and checkpointing works correctly.

#### Acceptance Criteria

1. THE ForgeState SHALL be a TypedDict with `total=False` containing identity fields (`session_id`, `status`, `build_mode`), routing fields (`intent`, `message`, `node_path`), planning fields (`tasks`, `task_ordering`, `current_task_index`, `current_task_id`), reference fields (`digital_twin`, `session_context`, `spec_artifact_uri`), result fields (`verification_results`, `commit_shas`, `decisions`, `errors`, `doc_updates`), and control flags (`needs_clarification`, `all_tasks_done`, `approval_pending`).
2. WHEN a node function returns a partial dict, THE ForgeState SHALL be updated by merging only the returned keys, preserving all other fields unchanged.
3. THE ForgeState SHALL store large objects by reference handle (string URI), not by embedding the full object.

### Requirement 2: Node Function Contract

**User Story:** As a workflow developer, I want each node function to follow a uniform async contract, so that the graph can invoke them interchangeably.

#### Acceptance Criteria

1. THE Node_Function SHALL accept a ForgeState dict and return a dict containing only the keys it modifies.
2. WHEN a Node_Function completes, THE Node_Function SHALL append its node name to the `node_path` list.
3. WHEN a Node_Function encounters an unhandled exception, THE Node_Function SHALL catch the exception, append an error entry to `errors`, and emit an error event via the EventBus.

### Requirement 3: Intake Node

**User Story:** As the workflow entry point, I want to validate and initialize incoming state, so that downstream nodes can rely on well-formed input.

#### Acceptance Criteria

1. WHEN the intake_node receives state with a non-empty `message` and valid `session_id`, THE intake_node SHALL set `status` to "processing" and append "intake" to `node_path`.
2. IF the `message` field is empty or missing, THEN THE intake_node SHALL append an error to `errors` with code "invalid_input" and set `status` to "failed".
3. WHEN the intake_node succeeds, THE intake_node SHALL write a checkpoint via CrashRecovery.

### Requirement 4: Classify Node

**User Story:** As a workflow orchestrator, I want to classify user intent deterministically, so that messages are routed to the correct sub-workflow.

#### Acceptance Criteria

1. WHEN the classify_node receives a processed message, THE classify_node SHALL set `intent` to one of "build_intent", "status_query", "interrupt", or "natural_language".
2. THE classify_node SHALL produce the same `intent` for the same (`message`, `build_mode`) pair (deterministic).
3. WHEN the intent is "status_query" or "interrupt", THE classify_node SHALL NOT invoke any AI model provider.

### Requirement 5: Clarify Node

**User Story:** As a user, I want the system to ask clarifying questions when my request is ambiguous, so that the build specification is complete before execution.

#### Acceptance Criteria

1. WHEN all required specification inputs are present, THE clarify_node SHALL set `needs_clarification` to False and allow the workflow to advance.
2. WHEN required specification inputs are missing, THE clarify_node SHALL emit clarification question events and set `needs_clarification` to True.
3. WHEN clarification answers are received, THE clarify_node SHALL record them in the session context before returning.

### Requirement 6: Architect Node

**User Story:** As a workflow orchestrator, I want to produce a build specification and task list from the clarified intent, so that execution can proceed with a plan.

#### Acceptance Criteria

1. WHEN invoked with `needs_clarification` == False, THE architect_node SHALL produce a specification artifact and store its URI in `spec_artifact_uri`.
2. WHEN invoked, THE architect_node SHALL populate `tasks` with a list of Task objects derived from the specification.
3. WHEN the architect_node completes, THE architect_node SHALL emit `spec.ready` and `tasks.ready` events.

### Requirement 7: Plan Node

**User Story:** As the workflow orchestrator, I want tasks ordered by dependency, so that execution respects prerequisites.

#### Acceptance Criteria

1. WHEN the plan_node receives a non-empty `tasks` list, THE plan_node SHALL compute a topological ordering and store it in `task_ordering`.
2. WHEN the plan_node succeeds, THE plan_node SHALL initialize `current_task_index` to 0.
3. IF a dependency cycle is detected, THEN THE plan_node SHALL set `status` to "plan_failed" and populate `errors` with cycle details.

### Requirement 8: Execute Node

**User Story:** As the workflow orchestrator, I want each task executed in an isolated workspace, so that task outputs don't interfere with each other.

#### Acceptance Criteria

1. WHEN the execute_node is invoked, THE execute_node SHALL create an isolated workspace for the current task and dispatch execution to the assigned tool.
2. WHEN execution completes, THE execute_node SHALL update the current task's status to "verifying".
3. WHEN workspace creation succeeds, THE execute_node SHALL emit a `workspace.created` event.

### Requirement 9: Verify Node

**User Story:** As a quality gate, I want task output verified against configured stages, so that only passing work proceeds to commit.

#### Acceptance Criteria

1. WHEN all blocking verification stages pass, THE verify_node SHALL emit a `verify.passed` event and update `verification_results`.
2. WHEN any blocking verification stage fails, THE verify_node SHALL record the failure in `verification_results` including the failing stage name.
3. THE verify_node SHALL run verification stages in the order defined by the pipeline configuration.

### Requirement 10: Commit Node

**User Story:** As the workflow orchestrator, I want verified changes committed and tracked, so that the VCS reflects each completed task.

#### Acceptance Criteria

1. WHEN invoked after verification passes, THE commit_node SHALL commit changes via the VCS connector and append the SHA to `commit_shas`.
2. WHEN a commit succeeds, THE commit_node SHALL emit a `commit.done` event and increment `current_task_index`.
3. WHEN all tasks are committed, THE commit_node SHALL set `all_tasks_done` to True.

### Requirement 11: Doc Update Node

**User Story:** As a documentation maintainer, I want documentation updated after commits, so that docs stay synchronized with code changes.

#### Acceptance Criteria

1. WHEN invoked with non-empty `commit_shas`, THE doc_update_node SHALL invoke the DocWriter to update documentation files.
2. WHEN documentation updates complete, THE doc_update_node SHALL populate `doc_updates` with the list of changed file paths.
3. IF the DocWriter capability is unavailable, THEN THE doc_update_node SHALL record a documentation drift warning and continue without failing.

### Requirement 12: Finalize Node

**User Story:** As the workflow orchestrator, I want the build finalized with a VCS push and learning record, so that the session completes cleanly.

#### Acceptance Criteria

1. WHEN invoked with `all_tasks_done` == True, THE finalize_node SHALL push committed changes to the remote VCS.
2. WHEN finalization completes, THE finalize_node SHALL emit a `build.done` event and set `status` to "completed".
3. WHEN finalization completes, THE finalize_node SHALL record learning outcomes via the LearningRecorder.

### Requirement 13: Conditional Routing

**User Story:** As the graph designer, I want conditional edges that route state to the correct next node, so that the workflow branches correctly.

#### Acceptance Criteria

1. WHEN `intent` is "build_intent", THE Routing_Function SHALL route to the clarify node.
2. WHEN `intent` is "status_query", THE Routing_Function SHALL route to the status node.
3. WHEN `intent` is "interrupt", THE Routing_Function SHALL route to the interrupt node.
4. WHEN verification passes, THE Routing_Function SHALL route to the commit node.
5. WHEN verification fails, THE Routing_Function SHALL route to the policy node.
6. WHEN a commit completes and `all_tasks_done` is False, THE Routing_Function SHALL route back to the execute node.
7. WHEN a commit completes and `all_tasks_done` is True, THE Routing_Function SHALL route to the doc_update node.

### Requirement 14: Graph Construction

**User Story:** As a workflow developer, I want the graph assembled from a single builder function, so that the topology is defined in one place and easy to test.

#### Acceptance Criteria

1. THE Graph_Builder SHALL register all node functions (intake, classify, clarify, architect, plan, execute, verify, policy, commit, doc_update, finalize, status, interrupt) in the StateGraph.
2. THE Graph_Builder SHALL set "intake" as the entry point.
3. THE Graph_Builder SHALL wire conditional edges for classify, verify, policy, and commit routing.
4. THE Graph_Builder SHALL return a compiled graph that accepts ForgeState via `ainvoke()`.

### Requirement 15: Bootstrap Sequence

**User Story:** As a system operator, I want the runtime to self-initialize at startup, so that capabilities are discovered and health is assessed before accepting work.

#### Acceptance Criteria

1. WHEN bootstrap runs, THE Bootstrap SHALL execute discovery to probe all configured resources concurrently.
2. WHEN discovery completes, THE Bootstrap SHALL register healthy capabilities in the Registry.
3. WHEN registration completes, THE Bootstrap SHALL start the HealthMonitor background task.
4. WHEN the HealthMonitor starts, THE Bootstrap SHALL evaluate the operational mode and emit a `forge.ready` event.
5. IF configuration validation fails, THEN THE Bootstrap SHALL halt startup and emit a configuration error event without starting the application server.

### Requirement 16: Application Entry Point

**User Story:** As a deployer, I want a single `create_app()` function that returns a configured FastAPI application, so that the service can be started with a standard uvicorn command.

#### Acceptance Criteria

1. THE Application_Entry_Point SHALL instantiate all runtime components and assemble a RuntimeDeps container.
2. THE Application_Entry_Point SHALL register a FastAPI lifespan that runs bootstrap on startup and stops the HealthMonitor on shutdown.
3. THE Application_Entry_Point SHALL expose an API route that invokes the compiled graph with a user-provided message.
4. THE Application_Entry_Point SHALL return a configured FastAPI instance suitable for `uvicorn app.workflow.app:app`.
