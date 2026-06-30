# Implementation Plan: Forge Adapters + Infrastructure

## Overview

This plan implements the concrete adapter layer and persistence infrastructure for the Forge runtime. It connects the existing protocol interfaces (`AIProvider`, `VCSConnector`, `CodingTool` in `app/runtime/protocols.py`) to real backends (OpenRouter, GitHub, Aider) and replaces in-memory stores with PostgreSQL-backed persistence via asyncpg. Docker Compose provides the local development environment.

Work is organized in three waves for parallelism — Wave 1 (adapters) tasks are independent of each other; Wave 2 (persistence) depends on Docker being available; Wave 3 (integration wiring) connects everything.

## Tasks

- [ ] 1. Docker & database foundation
  - [ ] 1.1 Create Dockerfile for the backend
    - Add `backend/Dockerfile` — multi-stage build (deps → app) using python:3.11-slim
    - Expose port 8000, run via uvicorn
    - _Requirements: Docker Setup_
  - [ ] 1.2 Create docker-compose.yml with postgres + forge-api services
    - Add `docker-compose.yml` at repo root with services: `postgres` (postgres:16-alpine, port 5432, health check) and `forge-api` (build context ./backend, depends_on postgres healthy)
    - Add `.env.docker` template with DATABASE_URL, OPENROUTER_API_KEY, GITHUB_TOKEN, AIDER_MODEL placeholders
    - _Requirements: Docker Setup_
  - [ ] 1.3 Set up Alembic migrations scaffold
    - Add `alembic` to pyproject.toml dependencies
    - Run `alembic init` in `backend/`, configure `env.py` for async (asyncpg) with `DATABASE_URL` from env
    - _Requirements: PostgreSQL Persistence_
  - [ ] 1.4 Create initial migration: sessions, audit_log, checkpoints, learning_outcomes
    - `sessions` table: id (UUID PK), status, build_mode, created_at, updated_at, context_json
    - `audit_log` table: id (UUID PK), session_id (FK), seq (int), event_type, source, payload (JSONB), created_at, causation_id, correlation_id
    - `checkpoints` table: session_id (FK), node_id, highest_seq, state_json (JSONB), created_at
    - `learning_outcomes` table: id (UUID PK), session_id (FK), outcome_type, data (JSONB), created_at
    - _Requirements: PostgreSQL Persistence_

- [ ] 2. Checkpoint — verify Docker + migrations work
  - Bring up `docker-compose up -d`, run `alembic upgrade head`, confirm tables exist. Ask the user if questions arise.

- [ ] 3. OpenRouter AI Provider Adapter (Wave 1a)
  - [ ] 3.1 Implement `app/adapters/openrouter.py`
    - Class `OpenRouterProvider` implementing `AIProvider` protocol from `app/runtime/protocols.py`
    - `complete()`: POST to `https://openrouter.ai/api/v1/chat/completions` via httpx, parse response
    - `stream()`: Same endpoint with `stream=True`, yield SSE chunks as an AsyncIterator[str]
    - `health_check()`: GET `/api/v1/models` (lightweight liveness check), return `Health`
    - Reads `OPENROUTER_API_KEY` from env; accepts model name as parameter
    - Classify errors: 401/403 → raise `PermanentError` (from `app.runtime.router`); 429/5xx → raise generic Exception (transient, router will retry)
    - Rate-limit handling: parse `Retry-After` header on 429, expose as metadata
    - _Requirements: OpenRouter AI Provider Adapter_
  - [ ]* 3.2 Write unit tests for OpenRouterProvider
    - Mock httpx responses for: successful completion, streaming, 429 rate limit, 401 auth error, 500 transient error, timeout
    - Verify `PermanentError` raised on 401/403, generic exception on 5xx
    - Verify streaming yields correct token chunks
    - _Requirements: OpenRouter AI Provider Adapter_

- [ ] 4. GitHub VCS Adapter (Wave 1b — parallel with 3)
  - [ ] 4.1 Implement `app/adapters/github_vcs.py`
    - Class `GitHubVCS` implementing `VCSConnector` protocol from `app/runtime/protocols.py`
    - `clone()`: run `git clone --depth=1 --branch <ref> <url> <dest>` via asyncio.create_subprocess_exec; inject token into URL
    - `commit()`: stage files, `git commit -m <msg>`, return SHA from stdout
    - `push()`: `git push` subprocess
    - `read_file()`, `list_files()`, `get_log()`: subprocess wrappers over git commands
    - `health_check()`: validate `GITHUB_TOKEN` is set + a lightweight `git ls-remote` or HTTP HEAD to api.github.com
    - Reads `GITHUB_TOKEN` from env
    - _Requirements: GitHub VCS Adapter_
  - [ ]* 4.2 Write unit tests for GitHubVCS
    - Mock subprocess calls (asyncio.create_subprocess_exec) to verify command construction
    - Test clone injects token correctly into URL without logging it
    - Test commit parses SHA from subprocess stdout
    - Test health_check returns unhealthy when GITHUB_TOKEN is unset
    - _Requirements: GitHub VCS Adapter_

- [ ] 5. Aider Coding Tool Adapter (Wave 1c — parallel with 3, 4)
  - [ ] 5.1 Implement `app/adapters/aider_tool.py`
    - Class `AiderTool` implementing `CodingTool` protocol from `app/runtime/protocols.py`
    - `execute()`: spawn `aider --yes --model <AIDER_MODEL> --message <task_description>` as subprocess in the workspace directory
    - Capture stdout/stderr, parse exit code → `ToolResult(success=exit_code==0, ...)`
    - Respect configurable timeout (default 300s)
    - `health_check()`: check `aider --version` succeeds, return `Health`
    - Reads `AIDER_MODEL` from env (default: `claude-sonnet-4-20250514`)
    - _Requirements: Aider Coding Tool Adapter_
  - [ ]* 5.2 Write unit tests for AiderTool
    - Mock subprocess to verify correct command construction (--yes, --model, --message flags)
    - Test success path returns ToolResult(success=True)
    - Test non-zero exit code returns ToolResult(success=False, error=stderr)
    - Test timeout kills subprocess and returns failure
    - _Requirements: Aider Coding Tool Adapter_

- [ ] 6. Checkpoint — adapters complete
  - Ensure all adapter tests pass. Ask the user if questions arise.

- [ ] 7. PostgreSQL persistence layer (Wave 2)
  - [ ] 7.1 Create `app/db/pool.py` — connection pool setup
    - Async context manager that creates an asyncpg pool from `DATABASE_URL` env var
    - Pool size from `DATABASE_POOL_SIZE` env (default 10)
    - Expose `get_pool()` for use in FastAPI lifespan
    - _Requirements: PostgreSQL Persistence_
  - [ ] 7.2 Implement `app/db/session_store.py` — SessionManager backed by postgres
    - Async CRUD: `create_session`, `get_session`, `update_session`, `list_sessions`
    - Same interface as existing in-memory `SessionManager` in `app/runtime/session/`
    - Uses asyncpg pool; parameterized queries for all inputs
    - _Requirements: PostgreSQL Persistence_
  - [ ] 7.3 Implement `app/db/audit_store.py` — AuditTrail backed by postgres
    - `record(event)`: INSERT into audit_log, assigning monotonic seq per session
    - `query(session_id, filters)`: SELECT with optional event_type/source/time-range filters
    - Same interface consumed by `RuntimeInspector`
    - _Requirements: PostgreSQL Persistence_
  - [ ] 7.4 Implement `app/db/checkpoint_store.py` — CrashRecovery checkpoint store
    - `write_checkpoint(session_id, node_id, highest_seq, state_json)`: UPSERT into checkpoints
    - `get_latest_checkpoint(session_id)`: SELECT latest
    - `list_non_terminal_sessions()`: SELECT sessions with status not in ('completed','failed')
    - Replaces `_InMemoryCheckpointStore` in `app/workflow/bootstrap.py`
    - _Requirements: PostgreSQL Persistence_
  - [ ] 7.5 Implement `app/db/learning_store.py` — LearningRecorder backed by postgres
    - `record_outcome(session_id, outcome_type, data)`: INSERT into learning_outcomes
    - `query_outcomes(filters)`: SELECT with session/type filtering
    - _Requirements: PostgreSQL Persistence_
  - [ ]* 7.6 Write integration tests for DB stores
    - Use a test postgres (docker or testcontainers) to verify CRUD operations
    - Test session_store round-trip, audit_store ordering guarantees, checkpoint upsert idempotency
    - _Requirements: PostgreSQL Persistence_

- [ ] 8. Checkpoint — persistence layer complete
  - Run migrations + integration tests against Docker postgres. Ensure all pass. Ask the user if questions arise.

- [ ] 9. Integration wiring (Wave 3)
  - [ ] 9.1 Update `app/workflow/bootstrap.py` to wire real adapters
    - Import `OpenRouterProvider`, `GitHubVCS`, `AiderTool`
    - Register them in the `CapabilityRegistry` during discovery
    - Replace `_noop_call_adapter` with `OpenRouterProvider.complete` passed as `ModelCallAdapter`
    - Replace `_InMemoryCheckpointStore` with `app.db.checkpoint_store`
    - Initialize asyncpg pool in FastAPI lifespan, pass to DB stores
    - _Requirements: All adapters + PostgreSQL Persistence_
  - [ ] 9.2 Update `.env.example` with new env vars
    - Add `OPENROUTER_API_KEY`, ensure `GITHUB_TOKEN`, `AIDER_MODEL`, `DATABASE_URL`, `DATABASE_POOL_SIZE` are documented
    - _Requirements: All_
  - [ ] 9.3 Add adapter registration to discovery config
    - Create/update `config/adapters.yaml` listing enabled adapters (openrouter, github, aider) so the discovery bootstrap probes them
    - _Requirements: All adapters_

- [ ] 10. Final checkpoint — full stack
  - `docker-compose up`, verify forge-api starts, health endpoint reports adapters healthy. Ensure all unit + integration tests pass. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for faster MVP
- Wave 1 (tasks 3, 4, 5) are fully parallel — no interdependencies
- Wave 2 (task 7) requires Docker/postgres from task 1 to be running
- Wave 3 (task 9) wires everything together
- All adapters implement protocols already defined in `app/runtime/protocols.py` — no protocol changes needed
- The `ModelCallAdapter` type alias in `app/runtime/router/__init__.py` is the interface the OpenRouter adapter's `complete` method must satisfy
- Secret values (tokens, API keys) are read from env and never logged or persisted in state/audit per existing `SecretHolder` pattern
