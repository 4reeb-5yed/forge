# Testing

Forge has 1,240+ tests covering the runtime, workflow, and adapters. The test suite uses pytest with Hypothesis for property-based testing.

## Test Organization

```
backend/tests/
├── test_event_bus.py              # EventBus unit tests
├── test_event_bus_properties.py   # EventBus property-based tests
├── test_registry.py               # CapabilityRegistry tests
├── test_registry_properties.py    # Registry property-based tests
├── test_router.py                 # ModelRouter tests
├── test_router_properties.py      # Router property-based tests
├── test_session.py                # SessionManager tests
├── test_audit.py                  # AuditTrail tests
├── test_budget.py                 # SessionBudget tests
├── test_budget_properties.py      # Budget property-based tests
├── test_policies.py               # PolicyEngine tests
├── test_policy_engine.py          # PolicyEngine extended tests
├── test_workspace.py              # WorkspaceManager tests
├── test_verification.py           # VerificationPipeline tests
├── test_health.py                 # HealthMonitor tests
├── test_discovery.py              # Discovery tests
├── test_inspector.py              # RuntimeInspector tests
├── test_interrupt.py              # InterruptHandler tests
├── test_recovery.py               # CrashRecovery tests
├── test_learning.py               # LearningRecorder tests
├── test_mode.py                   # ModeEvaluator tests
├── test_secrets.py                # SecretHolder tests
├── test_boundaries.py             # Boundary enforcement tests
├── test_classifier.py             # IntentClassifier tests
├── test_workflow_graph.py         # Graph construction tests
├── test_workflow_nodes.py         # Node function tests
├── test_workflow_routing.py       # Routing logic tests
├── test_adapters.py               # Adapter unit tests
├── test_api.py                    # API endpoint tests
└── conftest.py                    # Shared fixtures
```

### Test Categories

| Category | Pattern | Count | Purpose |
|----------|---------|-------|---------|
| Unit | `test_*.py` | ~1,100 | Isolated module behavior |
| Property-based | `test_*_properties.py` | ~140 | Invariant verification |
| Integration | `test_api.py`, `test_workflow_*.py` | ~50 | Cross-module interaction |

## How to Run Tests

```bash
cd backend

# Full suite (~5 minutes)
pytest

# Stop on first failure
pytest -x

# Verbose output
pytest -v

# Specific module
pytest tests/test_event_bus.py

# Specific test class
pytest tests/test_router.py::TestModelRouterBasicRouting

# Property-based tests only
pytest -k "properties"

# With coverage report
pytest --cov=app --cov-report=html

# Parallel execution (if pytest-xdist installed)
pytest -n auto
```

## Property-Based Testing with Hypothesis

Forge uses [Hypothesis](https://hypothesis.readthedocs.io/) to verify 12 correctness properties across core components. Property-based tests generate thousands of random inputs and verify that invariants always hold.

### The 12 Correctness Properties

#### Event Bus Properties

1. **Delivery guarantee** — Every published event is delivered to all matching subscribers exactly once
2. **Ordering guarantee** — Events are delivered in publication order within a session
3. **Wildcard correctness** — A `*` subscriber receives every event regardless of topic
4. **Unsubscribe isolation** — After unsubscribe, no further events are delivered to that handler

#### Registry Properties

5. **Register/deregister symmetry** — Registering then deregistering returns the registry to its prior state
6. **Kind-based lookup correctness** — `get_by_kind(K)` returns exactly the entries with `kind == K`
7. **Uniqueness invariant** — No two entries can share the same `name`

#### Budget Properties

8. **Monotonic consumption** — Remaining budget never increases (only decreases or stays)
9. **Boundary correctness** — `check(n)` returns False iff consuming `n` would exceed the limit
10. **Zero-budget safety** — A budget with limit=0 rejects all consumption

#### Router Properties

11. **Fallback chain exhaustion** — If all providers fail, the router raises (never silently drops)
12. **Provider selection determinism** — Given the same registry state and chain config, the same provider is always selected first

### Example Property Test

```python
from hypothesis import given, strategies as st

@given(
    events=st.lists(
        st.builds(Event.create, type=st.sampled_from(list(EventType)), ...),
        min_size=1,
        max_size=50,
    )
)
async def test_event_ordering_preserved(events):
    """Property: events delivered in publication order."""
    bus = EventBus()
    received = []
    bus.subscribe("*", lambda e: received.append(e), subscriber_id="test")
    
    for event in events:
        await bus.publish(event)
    
    assert received == events  # Order preserved
```

### Running Property Tests

```bash
# All property tests
pytest -k "properties"

# With more examples (slower but more thorough)
pytest -k "properties" --hypothesis-seed=0

# Show Hypothesis statistics
pytest -k "properties" --hypothesis-show-statistics
```

### Hypothesis Configuration

Tests use the default Hypothesis profile. The `.hypothesis/` directory in `backend/` stores the example database (previously-failing inputs that are re-tested on each run).

## Test Coverage

### Coverage Report

```bash
pytest --cov=app --cov-report=html --cov-report=term-missing
# Open htmlcov/index.html in browser
```

### Coverage Approach

- **Target:** High coverage on runtime modules (business logic)
- **Not targeted:** Adapter modules (they call external services) and generated code
- **Property tests supplement** unit tests — they find edge cases that manual tests miss

## How to Add New Tests

### Unit Test

```python
# tests/test_your_module.py
import pytest
from app.runtime.your_module import YourClass

class TestYourClass:
    """Tests for YourClass core behavior."""

    async def test_basic_operation(self):
        obj = YourClass(dep=FakeDep())
        result = await obj.do_thing("input")
        assert result.success is True

    async def test_error_handling(self):
        obj = YourClass(dep=FailingDep())
        with pytest.raises(YourModuleError):
            await obj.do_thing("bad input")
```

### Property Test

```python
# tests/test_your_module_properties.py
from hypothesis import given, strategies as st
from app.runtime.your_module import YourClass

class TestYourModuleProperties:
    """Correctness properties for YourModule."""

    @given(input_data=st.text(min_size=1, max_size=100))
    async def test_idempotency_property(self, input_data: str):
        """Property: calling do_thing twice with same input gives same result."""
        obj = YourClass(dep=FakeDep())
        r1 = await obj.do_thing(input_data)
        r2 = await obj.do_thing(input_data)
        assert r1 == r2
```

### Conventions

- **File naming:** `test_<module>.py` for unit, `test_<module>_properties.py` for properties
- **Test doubles:** Defined within each test file (fakes, not mocks)
- **Async tests:** Use `pytest-asyncio` with `asyncio_mode = "auto"` (no decorator needed)
- **No external calls:** Tests never hit real APIs — all adapters are faked
- **Fast:** The full suite runs in ~5 minutes on a modern machine
