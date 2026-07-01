# Implementation Plan: Forge Setup & Error Surfacing

## Overview

This plan implements the Configuration & Setup system and Error Surfacing system for Forge. The backend work spans the Runtime layer (ConfigService, error events) and Application layer (Config API endpoints, error middleware, enhanced health). The frontend work adds a Setup Wizard page, error surfaces (toasts, panel, banner), connection indicator, and WebSocket reconnection logic. Implementation builds on the existing EventBus, RuntimeDeps, auth system, and frontend API client.

## Tasks

- [x] 1. ConfigService Core
  - [x] 1.1 Create ConfigService with ConfigState model and persistence
    - Create `backend/app/runtime/config/__init__.py`
    - Define `SandboxMode` enum (always, auto, never)
    - Define `ConfigState` dataclass with fields: openrouter_api_key, github_token, selected_model, sandbox_mode, model_cache_ttl_seconds
    - Add `configured` property (True when openrouter_api_key and selected_model are set)
    - Implement `ConfigService.__init__(config_path, event_emitter)`
    - Implement `load()` — read JSON file, deserialize to ConfigState, handle missing file (defaults), handle corrupt JSON (log + defaults + emit CONFIG_ERROR)
    - Implement `_save()` — atomic write (write to .tmp, os.rename to target), restrictive permissions (0600)
    - _Requirements: 1.1, 1.2, 1.3, 1.5_

  - [x] 1.2 Implement config get/update with validation and redaction
    - Implement `get_config()` — return dict with secrets masked (show only last 4 chars), include `configured` boolean
    - Implement `update_config(payload)` — validate required fields, reject invalid values with field-level errors, persist atomically, apply to runtime
    - Implement `_redact(value)` — mask secret strings showing only last 4 chars (e.g. "sk-****abcd")
    - Raise `ConfigValidationError` with per-field error details on invalid payloads
    - _Requirements: 1.3, 1.4, 1.6_

  - [x] 1.3 Implement apply_to_runtime for hot-reload of configuration
    - Implement `apply_to_runtime(deps)` — update model router chain config with new model, update sandbox mode policy
    - Ensure no restart required after PUT /config
    - _Requirements: 1.3_

  - [x] 1.4 Write unit tests for ConfigService
    - Test load from valid JSON file
    - Test load with missing file returns defaults
    - Test load with corrupt JSON returns defaults and emits error
    - Test atomic save (verify .tmp + rename pattern)
    - Test secret redaction (various key formats)
    - Test update validation rejects invalid payloads
    - Test config round-trip: save then load preserves all fields

- [x] 2. API Key Testing
  - [x] 2.1 Implement key testing for OpenRouter
    - Make GET request to OpenRouter `/api/v1/models` with provided key in Authorization header
    - 10-second timeout via httpx
    - On success: return KeyTestResult(success=True, latency_ms, details={models_available: count})
    - On auth error (401/403): return KeyTestResult(success=False, error="Invalid credentials")
    - On timeout: return KeyTestResult(success=False, error="Request timed out after 10 seconds")
    - _Requirements: 3.1, 3.3, 3.4, 3.5_

  - [x] 2.2 Implement key testing for GitHub
    - Make GET request to GitHub `/user` with Bearer token
    - 10-second timeout via httpx
    - On success: return KeyTestResult(success=True, latency_ms, details={username: str})
    - On auth error: return KeyTestResult(success=False, error="Invalid credentials")
    - On timeout: return KeyTestResult(success=False, error="Request timed out after 10 seconds")
    - _Requirements: 3.2, 3.3, 3.4_

  - [x] 2.3 Write unit tests for key testing
    - Mock httpx responses for success, auth error, timeout for both OpenRouter and GitHub
    - Verify latency_ms is captured correctly
    - Verify error messages match documented format

- [ ] 3. Model List and Docker Probe
  - [~] 3.1 Implement model list fetching and caching
    - Implement `get_models()` — fetch from OpenRouter API, parse response into list of {id, name, context_length}
    - Cache model list with configurable TTL (default 3600s)
    - Return cached results on subsequent calls until TTL expires
    - Refresh on next call after expiry
    - Return ErrorEnvelope when OpenRouter key not configured
    - _Requirements: 4.1, 4.3, 4.4, 4.5_

  - [~] 3.2 Implement model validation in update_config
    - When selected_model is provided in PUT /config, validate it exists in cached model list
    - Reject with ErrorEnvelope if model not found in list
    - _Requirements: 4.2_

  - [~] 3.3 Implement Docker socket probe
    - Check if `/var/run/docker.sock` exists and is accessible
    - Check if `forge-aider-sandbox:latest` image is present via Docker socket API or subprocess
    - When sandbox_mode is "always" and Docker unavailable: report configuration error on health
    - When sandbox_mode is "auto": report Docker as informational (no error when unavailable)
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [~] 3.4 Write unit tests for model caching and Docker probe
    - Test cache hit, cache miss, cache expiry
    - Test Docker socket present/absent scenarios
    - Test sandbox_mode interactions with Docker availability

- [ ] 4. Error Envelope and Exception Handlers
  - [x] 4.1 Create ErrorEnvelope model and ErrorCategory enum
    - Create `backend/app/api/errors.py`
    - Define `ErrorCategory` enum: configuration, runtime, workflow, connection
    - Define `ErrorEnvelope` dataclass with code, message, category, recoverable, timestamp, suggestion
    - Implement `to_dict()` serialization method
    - Define `ConfigValidationError` exception class

  - [~] 4.2 Implement FastAPI exception handlers
    - Handler for `HTTPException` — map status codes to error codes, determine category from context
    - Handler for `ConfigValidationError` — return 422 with field-level error details
    - Handler for unhandled `Exception` — log traceback, return INTERNAL_ERROR envelope (no internal details)
    - Add `suggestion` field for configuration-category errors
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [~] 4.3 Register exception handlers in app factory
    - Add handlers to `create_app()` in `backend/app/api/__init__.py`
    - Ensure all existing endpoints now return ErrorEnvelope on errors
    - Verify no endpoint returns bare string or unstructured error body

  - [~] 4.4 Write unit tests for error handling
    - Test HTTPException → ErrorEnvelope mapping for various status codes
    - Test unhandled exception produces INTERNAL_ERROR without internals
    - Test ConfigValidationError produces 422 with field details
    - Test suggestion field present for configuration errors

- [ ] 5. Error Event Types
  - [~] 5.1 Add error event types to EventType enum
    - Add `CONFIG_ERROR = "error.config"` to EventType in `backend/app/runtime/events/models.py`
    - Add `RUNTIME_ERROR = "error.runtime"` to EventType
    - Add `WORKFLOW_ERROR = "error.workflow"` to EventType
    - _Requirements: 9.1, 9.2, 9.3, 9.5_

  - [~] 5.2 Implement error event emission in ConfigService
    - Emit CONFIG_ERROR on: invalid key detected, Docker unavailable with sandbox_mode=always, corrupt config file
    - Event payload: {code, message, category, component, recoverable, suggestion}
    - _Requirements: 9.1_

  - [~] 5.3 Verify WebSocket forwarding of error events
    - Write integration test: emit error event → verify WebSocket client receives it
    - Confirm existing event stream infrastructure handles new event types without changes
    - _Requirements: 9.4_

- [ ] 6. Config API Endpoints
  - [~] 6.1 Create config API route module with Pydantic models
    - Create `backend/app/api/config.py`
    - Define `ConfigUpdateRequest`, `KeyTestRequest`, `ConfigResponse` Pydantic models
    - Define APIRouter with prefix `/config`

  - [~] 6.2 Implement GET /config and PUT /config endpoints
    - GET /config: require auth, delegate to ConfigService.get_config()
    - PUT /config: require auth, validate payload, delegate to ConfigService.update_config()
    - Return ErrorEnvelope on validation failures (422)
    - _Requirements: 2.1, 2.2, 2.6_

  - [~] 6.3 Implement POST /config/test endpoint
    - Require auth, validate component name (openrouter, github)
    - Return ErrorEnvelope for unknown component names with valid names listed
    - Delegate to ConfigService.test_key()
    - _Requirements: 2.3, 2.5, 2.6_

  - [~] 6.4 Implement GET /config/health and GET /config/models endpoints
    - GET /config/health: require auth, delegate to ConfigService.get_component_health()
    - GET /config/models: require auth, delegate to ConfigService.get_models()
    - _Requirements: 2.4, 2.6_

  - [~] 6.5 Mount config router in app factory and wire dependencies
    - Register config router in `create_app()` in `backend/app/api/__init__.py`
    - Add ConfigService to AppDependencies
    - Wire from RuntimeDeps during lifespan startup

  - [~] 6.6 Write integration tests for config endpoints
    - Test all endpoints require auth (401 without token)
    - Test GET /config returns redacted values
    - Test PUT /config with valid and invalid payloads
    - Test POST /config/test with valid and invalid component names
    - Test GET /config/models when key is/isn't configured

- [ ] 7. Enhanced Health Endpoint
  - [~] 7.1 Implement GET /health with per-component status
    - No authentication required
    - Return: overall status, configured boolean, components object with ComponentHealth per component
    - Components: openrouter, github, docker, database, event_bus
    - _Requirements: 7.1, 7.2, 7.6_

  - [~] 7.2 Implement health status aggregation logic
    - All healthy → overall "healthy"
    - Non-critical (github, docker) unhealthy → overall "degraded" with unhealthy names listed
    - Critical (openrouter, database) unhealthy → overall "unhealthy"
    - _Requirements: 7.3, 7.4, 7.5_

  - [~] 7.3 Write unit tests for health endpoint
    - Test all-healthy scenario
    - Test degraded scenario (non-critical down)
    - Test unhealthy scenario (critical down)
    - Test no-auth access allowed
    - Test configured boolean reflects ConfigService state

- [ ] 8. Bootstrap Wiring
  - [~] 8.1 Add ConfigService to RuntimeDeps
    - Add `config_service: Any = None` field to RuntimeDeps in `backend/app/workflow/deps.py`

  - [~] 8.2 Wire ConfigService in assemble_deps and bootstrap
    - In `assemble_deps()`: instantiate ConfigService with path from FORGE_CONFIG_PATH env var (default `/data/forge-config.json`)
    - In `bootstrap()`: call `config_service.load()`, apply loaded config to model router and sandbox mode
    - In lifespan: pass config_service through to AppDependencies

  - [~] 8.3 Write integration test for bootstrap with ConfigService
    - Test bootstrap loads config and applies to runtime
    - Test bootstrap handles missing config file gracefully

- [ ] 9. Frontend Setup Wizard
  - [~] 9.1 Create Setup Wizard page with multi-step layout
    - Create `frontend/app/setup/page.tsx`
    - Three-step wizard: API Keys → Model Selection → Sandbox Mode
    - Step indicator showing progress

  - [~] 9.2 Implement API key step with test functionality
    - OpenRouter key input with "Test" button
    - GitHub token input (optional) with "Test" button
    - Display test results inline: success with latency, or failure with error message
    - _Requirements: 6.3_

  - [~] 9.3 Implement model selection and sandbox mode steps
    - Fetch models from GET /config/models, display in dropdown
    - Sandbox mode radio buttons (always/auto/never) with Docker status display
    - _Requirements: 6.2_

  - [~] 9.4 Implement save, redirect, and pre-population logic
    - "Save & Continue" calls PUT /config, redirects to `/` on success
    - Show ErrorEnvelope inline on failure (no navigation away)
    - Pre-populate with GET /config values on mount if already configured
    - _Requirements: 6.4, 6.5, 6.6_

  - [~] 9.5 Add redirect logic for unconfigured state
    - In `frontend/app/page.tsx`: check GET /config on mount, redirect to /setup if configured is false
    - _Requirements: 6.1_

- [ ] 10. Frontend API Client Extensions
  - [~] 10.1 Add config and health API functions to frontend/lib/api.ts
    - Add TypeScript interfaces: ErrorEnvelope, ComponentHealth, HealthResponse, ConfigResponse, KeyTestResult
    - Add functions: getConfig(), updateConfig(), testConfigKey(), getConfigHealth(), getConfigModels(), getHealth()
    - getHealth() should not include auth headers

- [ ] 11. Frontend Connection Indicator
  - [~] 11.1 Create ConnectionIndicator component
    - Create `frontend/components/ConnectionIndicator.tsx`
    - Colored dot: green (connected + healthy), yellow (degraded), red (disconnected or unreachable)
    - _Requirements: 10.1, 10.4_

  - [~] 11.2 Create health polling hook
    - Create `frontend/lib/health.ts` with `useHealthPolling(interval)` hook
    - Default 30-second polling interval
    - _Requirements: 10.5_

  - [~] 11.3 Implement state transitions with debouncing
    - WebSocket disconnect → red within 2 seconds
    - WebSocket reconnect → green within 2 seconds
    - Health degraded → yellow
    - _Requirements: 10.2, 10.3_

  - [~] 11.4 Integrate into StatusBar
    - Add ConnectionIndicator to `frontend/components/StatusBar.tsx`

- [ ] 12. Frontend Error Toast System
  - [~] 12.1 Create Error Toast component and state store
    - Create `frontend/components/ErrorToast.tsx` with message, suggestion, dismiss button
    - Create `frontend/lib/error-store.ts` with addError(), getErrors(), filterByCategory()
    - Max 200 entries, oldest evicted on overflow
    - _Requirements: 11.1, 12.1_

  - [~] 12.2 Implement toast stacking and auto-dismiss
    - Max 5 visible toasts, newest at top
    - Auto-dismiss after 8 seconds (pause on hover, immediate on click)
    - Overflow shows summary toast with count and link to Error Panel
    - _Requirements: 11.2, 11.3, 11.4, 11.5_

  - [~] 12.3 Wire toast display to WebSocket error events
    - When error.* event received with recoverable=true, show toast
    - _Requirements: 11.1_

- [ ] 13. Frontend Error Panel
  - [~] 13.1 Create Error Panel component
    - Create `frontend/components/ErrorPanel.tsx`
    - Scrollable list: error code, message, category badge, suggestion, locale-formatted timestamp
    - _Requirements: 12.1, 12.2_

  - [~] 13.2 Implement filtering and interaction
    - Category filter buttons (configuration, runtime, workflow, connection)
    - Click suggestion to highlight; configuration errors show link to Setup Wizard section
    - 200-entry cap with oldest-first eviction
    - _Requirements: 12.3, 12.4, 12.5_

  - [~] 13.3 Add Error Panel access to main interface
    - Add button/tab in StatusBar or sidebar to open Error Panel

- [ ] 14. Frontend Setup Banner
  - [~] 14.1 Create SetupBanner component
    - Create `frontend/components/SetupBanner.tsx`
    - Show "Forge is not fully configured" with link to /setup
    - Include unhealthy component names when status is degraded/unhealthy
    - _Requirements: 13.1, 13.2_

  - [~] 14.2 Implement health-driven visibility and auto-dismiss
    - Show when configured=false OR overall status is degraded/unhealthy
    - Disappear when health returns healthy + configured=true (no reload)
    - Persist across all pages via layout.tsx
    - _Requirements: 13.3, 13.4_

- [ ] 15. WebSocket Reconnection and Error Recovery
  - [~] 15.1 Implement exponential backoff reconnection
    - Enhance `connectEventStream()` in `frontend/lib/api.ts`
    - Backoff: 1s → 2s → 4s → 8s → 16s → 30s (max)
    - On disconnect: emit CONNECTION_ERROR to error store with timestamp and retry count
    - _Requirements: 14.1, 14.2_

  - [~] 15.2 Implement missed event recovery on reconnect
    - On reconnect: send last received seq to backend
    - Backend replays missed events via EventBus.replay(session_id, since_seq)
    - _Requirements: 14.3_

  - [~] 15.3 Implement API unreachable detection
    - If 3 consecutive health polls fail: show red indicator + toast "Forge API is unreachable"
    - On connectivity restored: clear error state, update indicator
    - _Requirements: 14.4, 14.5_

## Task Dependency Graph

```json
{
  "waves": [
    {"tasks": ["1", "4"], "description": "ConfigService core and ErrorEnvelope are independent foundations"},
    {"tasks": ["2", "5", "8"], "description": "Key testing needs ConfigService; Error events need ErrorEnvelope; Bootstrap wiring needs ConfigService"},
    {"tasks": ["3", "6", "10"], "description": "Models/Docker need key testing; Config API needs error events; Frontend API client is independent"},
    {"tasks": ["7"], "description": "Enhanced health needs Config API"},
    {"tasks": ["9", "11", "12", "13", "14"], "description": "Setup Wizard needs health + bootstrap; Frontend components need API client"},
    {"tasks": ["15"], "description": "Reconnection needs Connection Indicator + Error Toasts"}
  ]
}
```

## Notes

- All backend code uses Python 3.11+ with asyncio, FastAPI, and Pydantic.
- All frontend code uses Next.js 14, TypeScript, and Tailwind CSS.
- httpx is used for external HTTP calls (key testing) — already a project dependency via the OpenRouter adapter.
- The ConfigService follows the same pattern as other Runtime components: instantiated in assemble_deps(), wired into RuntimeDeps, accessed through dependency injection.
- Error events reuse the existing EventBus/WebSocket infrastructure — no new transport needed.
- The /health endpoint is deliberately unauthenticated for container orchestrator compatibility (Docker HEALTHCHECK, Kubernetes liveness probes).
