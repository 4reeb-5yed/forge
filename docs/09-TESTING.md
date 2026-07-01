# Testing

Forge has 1,352 tests covering the runtime, workflow, and adapters. The test suite uses pytest with Hypothesis for property-based testing.

## Test Organization

```
backend/tests/
├── test_adapters.py               # Adapter unit tests
├── test_api.py                    # API endpoint tests
├── test_audit.py                  # AuditTrail tests
├── test_audit_properties.py       # Audit property-based tests (replay fidelity)
├── test_auth.py                   # Authentication/authorization tests
├── test_backpressure.py           # Streaming backpressure / bounded queue tests
├── test_boundaries.py              # Boundary enforcement tests
├── test_budget.py                 # SessionBudget tests
├── test_budget_properties.py      # Budget property-based tests (budget safety)
├── test_circuit_breaker.py        # ProviderCircuitBreaker / backoff / retry tests
├── test_clarification.py          # Clarification workflow tests
├── test_classifier.py             # IntentClassifier tests
├── test_commit_finalization_docs.py  # Commit + finalization + documentation integration
├── test_commit_workflow.py        # CommitWorkflow tests
├── test_config_service.py         # ConfigService (get/update config, redaction) tests
├── test_dependency_wiring.py      # RuntimeDeps/bootstrap wiring regression tests
├── test_discovery.py              # Discovery tests
├── test_discovery_properties.py   # Discovery property-based tests (discovery soundness)
├── test_dispatcher.py             # TaskDispatcher / workspace isolation tests
├── test_documentation.py          # DocumentationMaintenance (digital twin diff) tests
├── test_event_bus.py              # EventBus unit tests
├── test_event_properties.py       # Event property-based tests (ordering, causality)
├── test_finalization.py           # Finalization node tests
├── test_health_monitor.py         # HealthMonitor tests
├── test_inspector.py              # RuntimeInspector tests
├── test_inspector_properties.py   # Inspector property-based tests (explainability)
├── test_intent_router.py          # Intent router tests
├── test_interrupt.py              # InterruptHandler tests
├── test_learning.py               # LearningRecorder tests
├── test_mode_evaluation.py        # ModeEvaluator tests
├── test_planner.py                # Planner tests
├── test_planner_properties.py     # Planner property-based tests (plan acyclicity)
├── test_policy_engine.py          # PolicyEngine tests
├── test_property_doc_drift.py     # Documentation property-based tests (non-drift)
├── test_protocols.py              # Plugin protocol interface / shared type tests
├── test_recovery.py               # CrashRecovery tests
├── test_registry.py               # CapabilityRegistry tests
├── test_router.py                 # ModelRouter tests
├── test_router_events.py          # ModelRouter event emission / budget integration tests
├── test_router_properties.py      # Router property-based tests (routing soundness, fallback)
├── test_sandboxed_aider.py        # SandboxedAiderTool (Docker sandbox) tests
├── test_scope_check.py            # scope_check.py diff-scope verification tests
├── test_secret_properties.py      # Secret property-based tests (non-leakage)
├── test_secrets.py                # SecretHolder tests
├── test_session.py                # SessionManager tests
├── test_specification.py          # Specification generation tests
├── test_verification.py           # VerificationPipeline tests
├── test_verification_properties.py  # Verification property-based tests (merge order-independence)
├── test_workflow_infra.py         # Workflow infra tests (routing, bootstrap, assemble_deps, app)
├── test_workflow_nodes.py         # Node function tests
├── test_workspace.py              # WorkspaceManager tests
└── test_workspace_properties.py   # Workspace property-based tests (isolation)
```

There is no `conftest.py` in `backend/tests/` — each test file defines its own fixtures/fakes inline.

### Test Categories

| Category | Pattern | Count | Purpose |
|----------|---------|-------|---------|
| Property-based | `test_*_properties.py` | 55 | Invariant verification |
| Unit + Integration | `test_*.py` (all others) | 1,297 | Isolated module behavior and cross-module interaction |

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

Forge uses [Hypothesis](https://hypothesis.readthedocs.io/) to verify correctness properties across core components. Property-based tests generate thousands of random inputs and verify that invariants always hold. There are 55 property-based tests total, spread across 11 `test_*_properties.py` files. Each file's docstring and test class names a numbered property (numbers are assigned per-topic and are not globally unique — e.g. two different files both use "Property 5" and "Property 8, 9" for unrelated invariants):

#### `test_router_properties.py` — Model Router

- **Property 1: Routing soundness** — `route()` returns the completion from the first provider that is present in the registry, breaker-closed, and succeeds; never returns from an unavailable or breaker-open provider
- **Property 7: Router fallback monotonicity** — `route()` tries providers strictly in chain order, never retries after a `PermanentError`, and raises `ModelUnavailableError` only after exhausting the chain

#### `test_discovery_properties.py` — Discovery

- **Property 2: Discovery soundness** — for every capability in the registry after bootstrap, its last `health_check` returned `ok=true`; no unhealthy capability is ever registered

#### `test_verification_properties.py` — Verification Pipeline

- **Property 3: Verification merge order-independence** — for all sets of advisory verifier results, the merged `dict[stage_name -> result]` is independent of wall-clock arrival order

#### `test_event_properties.py` — Event Bus

- **Property 4: Event ordering** — for all events sharing a `correlation_id`, `seq` values are unique and strictly increasing
- **Property 5: Causality closure** — for every non-root event, `causation_id` references an event with a smaller `seq` in the same session

#### `test_planner_properties.py` — Planner

- **Property 5: Plan acyclicity** — for all task sets, `plan` produces a DAG or reports a cycle; every `depends_on` references an existing task id

#### `test_audit_properties.py` — Audit Trail

- **Property 6: Audit replay fidelity** — replaying `audit_log` ordered by `seq` reconstructs a faithful projection: persisted events are field-for-field equal to the originals, in the same order, with no events lost or duplicated

#### `test_inspector_properties.py` — Runtime Inspector

- **Property 8: Explainability without inference** — every explain and runtime status response is derived solely from structured state (audit trail, registry, `ForgeState`); no LLM is invoked

#### `test_budget_properties.py` — Session Budget

- **Property 8: Budget safety** — no model call is issued when `session.budget.remaining < estimated_tokens`

#### `test_secret_properties.py` — Secret Holder

- **Property 9: Secret non-leakage** — for all persisted artifacts (state snapshots, audit records, event payloads), no raw VCS token or provider key appears after calling `redact_or_raise()`

#### `test_workspace_properties.py` — Workspace Manager

- **Property 9: Workspace isolation** — no worker writes to the canonical repository; all task work happens in an isolated workspace and reaches the canonical repo only via commit/merge

#### `test_property_doc_drift.py` — Documentation Maintenance

- **Property 10: Documentation non-drift** — after finalize, for every module changed in the twin diff, either docs were updated or a `DocDrift` entry is recorded

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
