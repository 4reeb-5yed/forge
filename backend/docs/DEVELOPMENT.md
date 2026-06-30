# Development Guide

## Setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
cp .env.example .env
```

## Running Tests

```bash
# Full suite (1,191 tests, ~4-5 minutes)
pytest

# With coverage
pytest --cov=app --cov-report=html

# Verbose output
pytest -v

# Stop on first failure
pytest -x

# Run a specific test file
pytest tests/test_event_bus.py

# Run a specific test class
pytest tests/test_router.py::TestModelRouterBasicRouting

# Property-based tests only (longer running)
pytest -k "properties"
```

## Code Style

```bash
# Lint
ruff check .

# Format
ruff format .
```

Configuration is in `pyproject.toml`:
- Target: Python 3.11
- Line length: 100
- Rules: E, F, I, N, W

## Project Conventions

### Module Structure
Each runtime module follows the same pattern:
- `__init__.py` — Main implementation (class + functions)
- One module = one responsibility
- Protocol interfaces for dependencies (dependency injection)

### Testing Pattern
- Unit tests in `tests/test_<module>.py`
- Property-based tests in `tests/test_<module>_properties.py`
- Test doubles (fakes, mocks) defined within each test file
- `pytest-asyncio` with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed)

### Event Emission
Every state-changing operation emits a typed event:
```python
event = Event.create(
    type=EventType.TASK_START,
    session_id=session_id,
    source="component_name",
    payload={"task_id": task_id, ...},
)
await event_emitter(event)
```

### Error Handling
- Custom exception classes per module (e.g., `SessionNotFoundError`, `BudgetExceededError`)
- Errors carry context (session_id, task_id, etc.)
- Operations that can fail emit error events before raising

### Configuration
- YAML files in `backend/config/`
- Environment variables in `.env` (never committed)
- All config values have sensible defaults
- Bounds clamping for numeric configs (e.g., probe_interval bounded 5–300s)

## Adding a New Module

1. Create `app/runtime/your_module/__init__.py`
2. Define the main class with typed constructor dependencies
3. Accept callbacks/protocols for external dependencies (not concrete implementations)
4. Emit events for observable state changes
5. Create `tests/test_your_module.py` with unit tests
6. Optionally create `tests/test_your_module_properties.py` for Hypothesis tests
7. Ensure the module only imports from its own layer or one adjacent layer

## Layer Boundary Rules

The boundary checker (`app/runtime/boundaries/`) enforces:
- Application layer cannot import engineering logic (router, verification, policies, etc.)
- Runtime layer cannot import HTTP/transport modules (fastapi, starlette, uvicorn)
- Adapter layer cannot import business logic (back-imports to runtime)
- Each module targets own layer or exactly one adjacent layer

Run the checker:
```python
from app.runtime.boundaries import enforce_boundaries
enforce_boundaries()  # raises BoundaryCheckError on violations
```

## Key Dependencies

| Package | Purpose |
|---------|---------|
| fastapi | Application layer HTTP/WebSocket |
| pydantic | Request/response validation |
| hypothesis | Property-based testing |
| pytest-asyncio | Async test support |
| pyyaml | Configuration parsing |
| httpx | HTTP client (testing + adapters) |
