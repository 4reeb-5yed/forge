# Requirements Document

## Introduction

Forge currently requires manual `.env` file editing for all configuration (API keys, model selection, sandbox mode) and surfaces runtime errors only as generic HTTP 500 responses or server-side log entries invisible to operators. This feature adds two complementary capabilities: (1) a Configuration & Setup system with a first-run wizard, backend config API, and persistent JSON storage so operators can configure Forge through the UI; and (2) a comprehensive Error Surfacing system that exposes structured, categorized errors through the API, WebSocket event stream, and frontend components in real time so operators can diagnose and resolve issues without inspecting server logs.

## Glossary

- **Config_Service**: The backend component responsible for loading, validating, persisting, and serving Forge configuration (API keys, model selection, sandbox mode) via a JSON file on a mounted volume.
- **Config_API**: The set of REST endpoints (GET/PUT /config, GET /config/health, POST /config/test) exposed by the Application_Layer for configuration management.
- **Setup_Wizard**: The frontend page at `/setup` that guides operators through first-time Forge configuration, including API key entry, model selection, and sandbox mode selection.
- **Error_Envelope**: A structured JSON response body containing `code`, `message`, `category`, `recoverable`, `suggestion`, and `timestamp` fields, returned on all API error responses.
- **Error_Category**: One of four classification buckets for errors: `configuration`, `runtime`, `workflow`, or `connection`.
- **Error_Panel**: The frontend component that displays a scrollable list of recent errors with code, message, suggestion, and timestamp.
- **Error_Toast**: A transient frontend notification for short-lived errors that auto-dismisses after a configurable duration.
- **Connection_Indicator**: A colored dot (green/yellow/red) displayed in the frontend top bar representing the current WebSocket connection health.
- **Setup_Banner**: A frontend banner shown when the enhanced health endpoint reports unhealthy components, directing operators to the Setup_Wizard.
- **Component_Health**: A per-component health status (healthy/degraded/unhealthy) with an optional error message, reported by the enhanced /health endpoint.
- **Config_File**: The JSON file on a mounted volume where the Config_Service persists configuration across restarts.
- **Key_Test**: A verification operation that confirms an API key is valid by making a lightweight probe request to the corresponding external service.
- **Application_Layer**: The FastAPI HTTP/WebSocket boundary that creates sessions, ingests messages, triggers the workflow, and broadcasts events.
- **Event_Bus**: The in-process, typed publish/subscribe channel; the single source of truth for everything that happens in a session.
- **Presentation_Layer**: The Next.js web client that renders chat, event log, status, and now configuration and error surfaces.
- **OpenRouter_Adapter**: The adapter that communicates with the OpenRouter API for AI model completions.

## Requirements

### Requirement 1: Configuration Persistence

**User Story:** As an operator, I want Forge configuration to be stored in a persistent file on a mounted volume, so that settings survive container restarts without requiring environment variable edits.

#### Acceptance Criteria

1. WHEN the Config_Service starts and a Config_File exists at the configured volume path, THE Config_Service SHALL load configuration values from that file and apply them to the runtime.
2. WHEN the Config_Service starts and no Config_File exists at the configured volume path, THE Config_Service SHALL operate with default values and SHALL report an unconfigured state on the health endpoint.
3. WHEN the Config_API receives a valid PUT /config request, THE Config_Service SHALL validate the payload, persist the configuration atomically to the Config_File, and apply updated values to the runtime without requiring a restart.
4. IF the Config_API receives a PUT /config request with missing required fields or invalid values, THEN THE Config_Service SHALL reject the request with an Error_Envelope identifying each invalid field and SHALL NOT modify the persisted Config_File.
5. WHEN the Config_File is written, THE Config_Service SHALL use atomic file operations (write to temporary file, then rename) to prevent corruption from interrupted writes.
6. THE Config_Service SHALL redact secret values (API keys, tokens) in all GET /config responses, returning only a masked representation sufficient to confirm a key is set.

### Requirement 2: Configuration API Endpoints

**User Story:** As a frontend client, I want RESTful endpoints to read, update, and test configuration, so that the Setup_Wizard can manage Forge settings programmatically.

#### Acceptance Criteria

1. WHEN a GET /config request is received, THE Config_API SHALL return the current configuration state with secret fields redacted and a boolean indicating whether Forge is fully configured.
2. WHEN a PUT /config request is received with valid fields, THE Config_API SHALL update the configuration and return the updated (redacted) state with an HTTP 200 response.
3. WHEN a POST /config/test request is received with a `component` field identifying the service to test, THE Config_API SHALL execute a Key_Test for that component and return a result containing `success`, `latency_ms`, and an optional `error` message.
4. WHEN a GET /config/health request is received, THE Config_API SHALL return a Component_Health status for each configured component (openrouter, github, docker, database) including health state and any error messages.
5. IF a POST /config/test request specifies an unknown component name, THEN THE Config_API SHALL return an Error_Envelope with a `configuration` category error identifying the valid component names.
6. THE Config_API SHALL require authentication (Bearer token) on all configuration endpoints consistent with existing Forge API authentication.

### Requirement 3: API Key Verification

**User Story:** As an operator, I want to test that my API keys work before saving configuration, so that I can catch typos and expired credentials immediately.

#### Acceptance Criteria

1. WHEN a Key_Test is requested for the OpenRouter component, THE Config_Service SHALL make a lightweight request to the OpenRouter API (model list endpoint) using the provided key and SHALL return success if a valid response is received within 10 seconds.
2. WHEN a Key_Test is requested for the GitHub component, THE Config_Service SHALL make an authenticated request to the GitHub user endpoint using the provided token and SHALL return success if authentication succeeds within 10 seconds.
3. IF a Key_Test request times out after 10 seconds, THEN THE Config_Service SHALL return a failure result with an error message indicating a timeout and the component name.
4. IF a Key_Test receives an authentication error from the external service, THEN THE Config_Service SHALL return a failure result with an error message indicating invalid credentials and SHALL NOT persist the failing key.
5. WHEN a Key_Test succeeds for the OpenRouter component, THE Config_Service SHALL include the account identifier or available model count in the success response to confirm correct account association.

### Requirement 4: Model Selection

**User Story:** As an operator, I want to select AI models from the list of models available on OpenRouter, so that I can choose cost-appropriate and capability-appropriate models for Forge roles.

#### Acceptance Criteria

1. WHEN the OpenRouter API key is configured and valid, THE Config_API SHALL provide a GET /config/models endpoint that returns the list of available models from the OpenRouter model list API.
2. WHEN an operator selects a model via PUT /config, THE Config_Service SHALL validate that the selected model identifier exists in the cached model list before persisting the selection.
3. IF the OpenRouter API key is not configured or invalid, THEN THE Config_API SHALL return an Error_Envelope on GET /config/models indicating that a valid OpenRouter key is required first.
4. THE Config_Service SHALL cache the model list for a configurable duration (default 1 hour) to avoid excessive calls to the OpenRouter API.
5. WHEN the cached model list has expired, THE Config_Service SHALL refresh the list on the next GET /config/models request and return the updated results.

### Requirement 5: Sandbox Mode Configuration

**User Story:** As an operator, I want to select the sandbox execution mode and see whether Docker is available, so that I can ensure secure AI code execution is properly configured.

#### Acceptance Criteria

1. WHEN an operator sets the sandbox mode to `always`, `auto`, or `never` via PUT /config, THE Config_Service SHALL persist the selection and apply it to the runtime sandbox policy.
2. WHEN a GET /config/health request is received, THE Config_Service SHALL include Docker availability status by probing the Docker socket and reporting whether the sandbox image (`forge-aider-sandbox:latest`) is present.
3. IF the sandbox mode is set to `always` and Docker is unavailable, THEN THE Config_Service SHALL report a `configuration` category error on the health endpoint indicating that the required Docker dependency is missing.
4. WHEN the sandbox mode is `auto`, THE Config_Service SHALL report Docker status as informational and SHALL NOT report an error when Docker is unavailable.

### Requirement 6: First-Run Setup Wizard

**User Story:** As an operator starting Forge for the first time, I want a guided setup page that walks me through configuration, so that I can get Forge running without reading documentation or editing files.

#### Acceptance Criteria

1. WHEN the Presentation_Layer loads and the GET /config response indicates Forge is not fully configured, THE Presentation_Layer SHALL redirect the operator to the Setup_Wizard page at `/setup`.
2. THE Setup_Wizard SHALL present configuration steps in sequential order: API key entry, model selection, and sandbox mode selection.
3. WHEN the operator enters an API key in the Setup_Wizard, THE Presentation_Layer SHALL provide a "Test" button that invokes POST /config/test and displays the result (success with latency, or failure with error message) adjacent to the input field.
4. WHEN all required configuration fields are populated and tested successfully, THE Setup_Wizard SHALL enable a "Save & Continue" action that invokes PUT /config and redirects to the main Forge interface on success.
5. IF the PUT /config call from the Setup_Wizard fails, THEN THE Presentation_Layer SHALL display the Error_Envelope message inline without navigating away from the Setup_Wizard.
6. WHEN the operator returns to `/setup` after Forge is already configured, THE Setup_Wizard SHALL pre-populate fields with current (redacted) values and allow editing.

### Requirement 7: Enhanced Health Endpoint

**User Story:** As an operator or monitoring system, I want the health endpoint to report per-component status with error messages, so that I can quickly identify which parts of Forge are misconfigured or failing.

#### Acceptance Criteria

1. WHEN a GET /health request is received, THE Application_Layer SHALL return a response containing an overall status (`healthy`, `degraded`, or `unhealthy`), a `components` object with per-component Component_Health entries, and a `configured` boolean.
2. THE enhanced /health endpoint SHALL include Component_Health entries for: `openrouter`, `github`, `docker`, `database`, and `event_bus`.
3. WHEN all components report healthy status, THE /health endpoint SHALL return an overall status of `healthy`.
4. WHEN at least one non-critical component is unhealthy, THE /health endpoint SHALL return an overall status of `degraded` with the unhealthy component names listed.
5. WHEN a critical component (openrouter, database) is unhealthy, THE /health endpoint SHALL return an overall status of `unhealthy`.
6. THE /health endpoint SHALL remain accessible without authentication so that external monitoring and container orchestrators can probe it.

### Requirement 8: Structured Error Responses

**User Story:** As a frontend client, I want all API errors to follow a consistent structure with actionable information, so that I can display meaningful error messages to operators.

#### Acceptance Criteria

1. THE Application_Layer SHALL return all error responses (4xx and 5xx) as an Error_Envelope containing: `code` (string identifier), `message` (human-readable description), `category` (Error_Category), `recoverable` (boolean), `suggestion` (optional remediation hint), and `timestamp` (ISO 8601).
2. WHEN an error is categorized as `configuration`, THE Error_Envelope SHALL include a `suggestion` field directing the operator toward the relevant configuration action.
3. WHEN an error is categorized as `runtime` and marked `recoverable`, THE Error_Envelope SHALL include a `suggestion` field indicating the recommended retry or recovery action.
4. THE Application_Layer SHALL map existing HTTPException responses to the Error_Envelope format so that no endpoint returns a bare string or unstructured error body.
5. IF an unhandled exception occurs, THEN THE Application_Layer SHALL catch the exception, log the full traceback server-side, and return an Error_Envelope with code `INTERNAL_ERROR`, category `runtime`, recoverable `false`, and a generic message without exposing internal details.

### Requirement 9: Error Events on Event Bus

**User Story:** As a real-time observer, I want errors to be emitted as typed events on the Event Bus, so that the WebSocket stream delivers error information to connected clients immediately.

#### Acceptance Criteria

1. WHEN a configuration error occurs (missing key, invalid token, Docker not found), THE Config_Service SHALL emit a `CONFIG_ERROR` event on the Event_Bus with the error code, message, component name, and recoverability.
2. WHEN a runtime error occurs (model router exhausted, workspace limit hit, sandbox crashed), THE Forge_Runtime SHALL emit a `RUNTIME_ERROR` event on the Event_Bus with the error code, message, affected session identifier, and recoverability.
3. WHEN a workflow error occurs (node failure, scope check blocked commit), THE Workflow_Engine SHALL emit a `WORKFLOW_ERROR` event on the Event_Bus with the error code, message, node name, session identifier, and recoverability.
4. WHEN a WebSocket client is connected to a session's event stream, THE Application_Layer SHALL forward all error events (CONFIG_ERROR, RUNTIME_ERROR, WORKFLOW_ERROR) for that session to the client in real time.
5. THE error events SHALL conform to the existing Event schema (type, payload, session_id, seq, timestamp, source, event_id) so that event consumers require no special handling beyond recognizing the new event types.

### Requirement 10: Frontend Connection Status Indicator

**User Story:** As an operator, I want to see the connection health at a glance in the UI, so that I know immediately when the WebSocket or API is unreachable.

#### Acceptance Criteria

1. THE Presentation_Layer SHALL display a Connection_Indicator in the top bar that shows green when the WebSocket is connected and the last API health check returned healthy.
2. WHEN the WebSocket connection is lost, THE Connection_Indicator SHALL transition to red within 2 seconds of the disconnect event.
3. WHEN the WebSocket reconnects after a disconnection, THE Connection_Indicator SHALL transition back to green within 2 seconds of the successful reconnection.
4. WHEN the /health endpoint returns `degraded` status, THE Connection_Indicator SHALL display yellow to indicate partial availability.
5. THE Presentation_Layer SHALL poll the /health endpoint at a configurable interval (default 30 seconds) to detect backend issues independent of the WebSocket state.

### Requirement 11: Frontend Error Toast System

**User Story:** As an operator, I want transient errors to appear as dismissible toast notifications, so that I'm aware of issues without interrupting my workflow.

#### Acceptance Criteria

1. WHEN a recoverable error event is received over the WebSocket, THE Presentation_Layer SHALL display an Error_Toast containing the error message and suggestion.
2. THE Error_Toast SHALL auto-dismiss after a configurable duration (default 8 seconds) unless the operator hovers over or clicks the toast.
3. WHEN the operator clicks a dismiss action on the Error_Toast, THE Presentation_Layer SHALL remove the toast immediately.
4. WHEN multiple error events arrive within the auto-dismiss window, THE Presentation_Layer SHALL stack toasts vertically with the newest at the top, limited to a maximum of 5 visible toasts.
5. IF more than 5 errors arrive within the auto-dismiss window, THEN THE Presentation_Layer SHALL display a summary toast indicating the overflow count and a link to the Error_Panel.

### Requirement 12: Frontend Error Panel

**User Story:** As an operator, I want a persistent error log panel showing all recent errors with details, so that I can investigate issues after transient toasts have dismissed.

#### Acceptance Criteria

1. THE Presentation_Layer SHALL provide an Error_Panel accessible from the main interface that lists all errors received during the current browser session.
2. WHEN an error event is received, THE Error_Panel SHALL prepend it to the list showing: error code, message, category, suggestion, and timestamp formatted in the operator's locale.
3. THE Error_Panel SHALL support filtering errors by Error_Category (configuration, runtime, workflow, connection).
4. WHEN an operator clicks an error entry in the Error_Panel that has a suggestion, THE Presentation_Layer SHALL highlight the suggestion text and, for configuration errors, offer a link to the relevant Setup_Wizard section.
5. THE Error_Panel SHALL retain a maximum of 200 error entries, discarding the oldest entries when the limit is exceeded.

### Requirement 13: Setup Banner for Unhealthy State

**User Story:** As an operator, I want a persistent banner when Forge is not fully configured, so that I'm reminded to complete setup before attempting builds.

#### Acceptance Criteria

1. WHEN the /health endpoint returns a `configured` value of false, THE Presentation_Layer SHALL display a Setup_Banner at the top of the main interface with the text "Forge is not fully configured" and a link to the Setup_Wizard.
2. WHEN the /health endpoint returns `degraded` or `unhealthy` overall status, THE Setup_Banner SHALL include the names of unhealthy components.
3. WHEN the operator completes configuration via the Setup_Wizard and /health returns `healthy` with `configured` true, THE Setup_Banner SHALL disappear without requiring a page reload.
4. THE Setup_Banner SHALL remain visible across all pages of the Presentation_Layer until the health condition is resolved.

### Requirement 14: Connection Error Detection and Recovery

**User Story:** As an operator, I want the frontend to detect and recover from connection losses gracefully, so that I don't lose context or miss errors during network interruptions.

#### Acceptance Criteria

1. WHEN the WebSocket connection is lost, THE Presentation_Layer SHALL attempt automatic reconnection using exponential backoff starting at 1 second, doubling up to a maximum interval of 30 seconds.
2. WHILE the WebSocket is disconnected, THE Presentation_Layer SHALL emit a `CONNECTION_ERROR` entry to the Error_Panel with the disconnection timestamp and retry count.
3. WHEN the WebSocket reconnects, THE Presentation_Layer SHALL request missed events from the backend by providing the last received sequence number so the event stream resumes without gaps.
4. IF the API is unreachable for 3 consecutive health polls, THEN THE Connection_Indicator SHALL display red and THE Presentation_Layer SHALL display an Error_Toast with message "Forge API is unreachable" and suggestion "Check that the backend is running."
5. WHEN API connectivity is restored after an unreachable period, THE Presentation_Layer SHALL clear the connection error state and transition the Connection_Indicator to the appropriate color based on the health response.
