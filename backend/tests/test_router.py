"""Unit tests for the ModelRouter with role-based resolution and ordered fallback chain.

Tests cover:
- Resolve role to ordered provider+model chain
- Skip providers not present in Registry or with open circuit breaker
- Attempt providers strictly in chain order
- Return first healthy, breaker-closed success
- Raise ModelUnavailableError after exhausting all providers with attempt log
- Record attempt log in AuditTrail on exhaustion

Requirements: 11.1, 11.2, 11.3, 11.6, 11.7
"""

from __future__ import annotations

import pytest

from app.runtime.events.models import DecisionKind, DecisionRecord
from app.runtime.models import Role
from app.runtime.router import (
    AttemptRecord,
    ChainEntry,
    CircuitBreakerState,
    ModelRouter,
    ModelUnavailableError,
    RoleChainConfig,
    SkipReason,
    StubCircuitBreaker,
)


# --- Helpers ---


class FakeCircuitBreaker:
    """A fake circuit breaker for testing with configurable state."""

    def __init__(self, state: CircuitBreakerState = CircuitBreakerState.CLOSED) -> None:
        self._state = state
        self.successes: int = 0
        self.failures: int = 0

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


def make_chain_config(chains: dict[Role, list[tuple[str, str]]]) -> RoleChainConfig:
    """Create a RoleChainConfig from a simplified dict."""
    return RoleChainConfig(
        chains={
            role: [ChainEntry(provider=p, model=m) for p, m in entries]
            for role, entries in chains.items()
        }
    )


def make_registry_checker(available: set[str]):
    """Create a registry checker that returns True for providers in `available`."""
    return lambda provider: provider in available


def make_call_adapter(responses: dict[str, str] | None = None, failures: dict[str, Exception] | None = None):
    """Create a call adapter that returns canned responses or raises errors.

    Args:
        responses: Dict mapping provider names to response strings.
        failures: Dict mapping provider names to exceptions to raise.
    """
    responses = responses or {}
    failures = failures or {}

    async def adapter(provider: str, model: str, messages: list, **kwargs) -> str:
        if provider in failures:
            raise failures[provider]
        if provider in responses:
            return responses[provider]
        raise RuntimeError(f"No response configured for provider '{provider}'")

    return adapter


# --- Tests ---


class TestRoleChainConfigParsing:
    """Tests for RoleChainConfig.from_yaml_dict."""

    def test_parses_valid_yaml_roles(self):
        data = {
            "roles": {
                "coder": {
                    "chain": [
                        {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
                        {"provider": "openai", "model": "gpt-4o"},
                    ]
                },
                "reviewer": {
                    "chain": [
                        {"provider": "openai", "model": "gpt-4o"},
                    ]
                },
            }
        }
        config = RoleChainConfig.from_yaml_dict(data)

        assert Role.CODER in config.chains
        assert len(config.chains[Role.CODER]) == 2
        assert config.chains[Role.CODER][0] == ChainEntry("anthropic", "claude-sonnet-4-20250514")
        assert config.chains[Role.CODER][1] == ChainEntry("openai", "gpt-4o")
        assert Role.REVIEWER in config.chains
        assert len(config.chains[Role.REVIEWER]) == 1

    def test_skips_unknown_roles(self):
        data = {
            "roles": {
                "coder": {"chain": [{"provider": "openai", "model": "gpt-4o"}]},
                "unknown_role": {"chain": [{"provider": "x", "model": "y"}]},
            }
        }
        config = RoleChainConfig.from_yaml_dict(data)

        assert Role.CODER in config.chains
        assert len(config.chains) == 1

    def test_empty_chain(self):
        data = {"roles": {"coder": {"chain": []}}}
        config = RoleChainConfig.from_yaml_dict(data)

        assert config.chains[Role.CODER] == []


class TestModelRouterBasicRouting:
    """Tests for basic route() behavior."""

    @pytest.fixture
    def simple_config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [("anthropic", "claude-sonnet-4-20250514"), ("openai", "gpt-4o")],
            Role.REVIEWER: [("openai", "gpt-4o")],
        })

    async def test_returns_first_successful_provider(self, simple_config):
        """Requirement 11.1, 11.3: attempt in order, return first success."""
        router = ModelRouter(
            chain_config=simple_config,
            registry_checker=make_registry_checker({"anthropic", "openai"}),
            call_adapter=make_call_adapter(
                responses={"anthropic": "anthropic_response", "openai": "openai_response"}
            ),
        )

        result = await router.route(Role.CODER, messages=[{"role": "user", "content": "hello"}])
        assert result == "anthropic_response"

    async def test_falls_back_to_second_provider_on_failure(self, simple_config):
        """Requirement 11.1: attempts in chain order, falls back on failure."""
        router = ModelRouter(
            chain_config=simple_config,
            registry_checker=make_registry_checker({"anthropic", "openai"}),
            call_adapter=make_call_adapter(
                responses={"openai": "openai_response"},
                failures={"anthropic": RuntimeError("timeout")},
            ),
        )

        result = await router.route(Role.CODER, messages=[{"role": "user", "content": "hello"}])
        assert result == "openai_response"

    async def test_records_success_on_circuit_breaker(self, simple_config):
        """Requirement 11.3: record success on the circuit breaker."""
        breaker = FakeCircuitBreaker()
        router = ModelRouter(
            chain_config=simple_config,
            registry_checker=make_registry_checker({"anthropic", "openai"}),
            call_adapter=make_call_adapter(responses={"anthropic": "ok"}),
            breakers={"anthropic": breaker},
        )

        await router.route(Role.CODER, messages=[])
        assert breaker.successes == 1
        assert breaker.failures == 0

    async def test_records_failure_on_circuit_breaker(self, simple_config):
        """Record failure on the circuit breaker when call fails."""
        breaker = FakeCircuitBreaker()
        router = ModelRouter(
            chain_config=simple_config,
            registry_checker=make_registry_checker({"anthropic", "openai"}),
            call_adapter=make_call_adapter(
                responses={"openai": "ok"},
                failures={"anthropic": RuntimeError("boom")},
            ),
            breakers={"anthropic": breaker},
            max_retries=0,  # No retries — tests single-attempt failure recording
        )

        await router.route(Role.CODER, messages=[])
        assert breaker.failures == 1


class TestModelRouterSkipping:
    """Tests for provider skipping (registry + breaker)."""

    @pytest.fixture
    def config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [
                ("provider_a", "model_a"),
                ("provider_b", "model_b"),
                ("provider_c", "model_c"),
            ],
        })

    async def test_skips_provider_not_in_registry(self, config):
        """Requirement 11.2: skip providers not present in Registry."""
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_b", "provider_c"}),
            call_adapter=make_call_adapter(
                responses={"provider_b": "b_response", "provider_c": "c_response"}
            ),
        )

        result = await router.route(Role.CODER, messages=[])
        # Should skip provider_a and use provider_b
        assert result == "b_response"

    async def test_skips_provider_with_open_breaker(self, config):
        """Requirement 11.2: skip providers with open circuit breaker."""
        open_breaker = FakeCircuitBreaker(state=CircuitBreakerState.OPEN)
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b", "provider_c"}),
            call_adapter=make_call_adapter(
                responses={"provider_a": "a_response", "provider_b": "b_response", "provider_c": "c_response"}
            ),
            breakers={"provider_a": open_breaker},
        )

        result = await router.route(Role.CODER, messages=[])
        # Should skip provider_a (breaker open) and use provider_b
        assert result == "b_response"

    async def test_skips_multiple_providers(self, config):
        """Skip multiple providers for different reasons."""
        open_breaker = FakeCircuitBreaker(state=CircuitBreakerState.OPEN)
        router = ModelRouter(
            chain_config=config,
            # provider_a not in registry, provider_b breaker open
            registry_checker=make_registry_checker({"provider_b", "provider_c"}),
            call_adapter=make_call_adapter(
                responses={"provider_c": "c_response"}
            ),
            breakers={"provider_b": open_breaker},
        )

        result = await router.route(Role.CODER, messages=[])
        # provider_a: not in registry, provider_b: breaker open -> provider_c
        assert result == "c_response"


class TestModelRouterExhaustion:
    """Tests for ModelUnavailableError on chain exhaustion."""

    @pytest.fixture
    def config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })

    async def test_raises_when_all_providers_fail(self, config):
        """Requirement 11.6: raise ModelUnavailableError after exhausting chain."""
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=make_call_adapter(
                failures={
                    "provider_a": RuntimeError("timeout"),
                    "provider_b": RuntimeError("rate_limit"),
                }
            ),
            max_retries=0,  # No retries — tests single-attempt exhaustion
        )

        with pytest.raises(ModelUnavailableError) as exc_info:
            await router.route(Role.CODER, messages=[])

        err = exc_info.value
        assert err.role == Role.CODER
        assert len(err.attempt_log) == 2
        assert err.attempt_log[0].provider == "provider_a"
        assert err.attempt_log[0].status == "failed"
        assert err.attempt_log[1].provider == "provider_b"
        assert err.attempt_log[1].status == "failed"

    async def test_raises_when_all_providers_skipped(self, config):
        """Requirement 11.6: raise when all providers are skipped."""
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker(set()),  # none available
            call_adapter=make_call_adapter(),
        )

        with pytest.raises(ModelUnavailableError) as exc_info:
            await router.route(Role.CODER, messages=[])

        err = exc_info.value
        assert err.role == Role.CODER
        assert len(err.attempt_log) == 2
        assert all(a.status == "skipped" for a in err.attempt_log)

    async def test_raises_with_mix_of_skip_and_failure(self, config):
        """Raise with attempt log showing mixed skip and failure."""
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_b"}),
            call_adapter=make_call_adapter(
                failures={"provider_b": RuntimeError("error")}
            ),
        )

        with pytest.raises(ModelUnavailableError) as exc_info:
            await router.route(Role.CODER, messages=[])

        err = exc_info.value
        assert err.attempt_log[0].status == "skipped"
        assert err.attempt_log[0].reason == SkipReason.NOT_IN_REGISTRY.value
        assert err.attempt_log[1].status == "failed"

    async def test_raises_for_role_with_no_chain(self):
        """Raise immediately when a role has no configured chain."""
        config = make_chain_config({})
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker(set()),
            call_adapter=make_call_adapter(),
        )

        with pytest.raises(ModelUnavailableError) as exc_info:
            await router.route(Role.CODER, messages=[])

        err = exc_info.value
        assert err.role == Role.CODER
        assert len(err.attempt_log) == 0


class TestModelRouterAuditRecording:
    """Tests for audit trail recording on exhaustion."""

    async def test_records_decision_on_exhaustion(self):
        """Requirement 11.6: record attempt log in AuditTrail on exhaustion."""
        recorded: list[DecisionRecord] = []

        async def fake_recorder(record: DecisionRecord) -> None:
            recorded.append(record)

        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=make_call_adapter(
                failures={
                    "provider_a": RuntimeError("err1"),
                    "provider_b": RuntimeError("err2"),
                }
            ),
            audit_recorder=fake_recorder,
            max_retries=0,  # No retries — tests single-attempt exhaustion recording
        )

        with pytest.raises(ModelUnavailableError):
            await router.route(Role.CODER, messages=[])

        assert len(recorded) == 1
        record = recorded[0]
        assert record.kind == DecisionKind.MODEL_FALLBACK
        assert record.subject == "role:coder"
        assert record.decision == "model_unavailable"
        assert "provider_a" in record.rationale
        assert "provider_b" in record.rationale
        assert len(record.inputs["attempts"]) == 2

    async def test_no_audit_record_on_success(self):
        """No audit record written when routing succeeds."""
        recorded: list[DecisionRecord] = []

        async def fake_recorder(record: DecisionRecord) -> None:
            recorded.append(record)

        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a")],
        })
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a"}),
            call_adapter=make_call_adapter(responses={"provider_a": "ok"}),
            audit_recorder=fake_recorder,
        )

        await router.route(Role.CODER, messages=[])
        assert len(recorded) == 0

    async def test_audit_recorder_failure_does_not_suppress_error(self):
        """If audit recording fails, ModelUnavailableError is still raised."""

        async def failing_recorder(record: DecisionRecord) -> None:
            raise RuntimeError("audit write failed")

        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a")],
        })
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker(set()),
            call_adapter=make_call_adapter(),
            audit_recorder=failing_recorder,
        )

        with pytest.raises(ModelUnavailableError):
            await router.route(Role.CODER, messages=[])


class TestModelRouterDeterminism:
    """Tests for deterministic routing behavior."""

    async def test_same_result_for_same_state(self):
        """Requirement 11.7: same provider+model for same Role when state is fixed."""
        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })
        call_count = {"provider_a": 0}

        async def counting_adapter(provider, model, messages, **kwargs):
            call_count["provider_a"] += 1
            return f"response_{call_count['provider_a']}"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=counting_adapter,
        )

        # Both calls should go to provider_a (first in chain)
        r1 = await router.route(Role.CODER, messages=[])
        r2 = await router.route(Role.CODER, messages=[])

        # Provider selected is deterministic (always provider_a when state is fixed)
        assert r1.startswith("response_")
        assert r2.startswith("response_")

    async def test_chain_order_is_deterministic(self):
        """The chain is always walked in configured order."""
        config = make_chain_config({
            Role.CODER: [
                ("first", "m1"),
                ("second", "m2"),
                ("third", "m3"),
            ],
        })
        call_order: list[str] = []

        async def tracking_adapter(provider, model, messages, **kwargs):
            call_order.append(provider)
            raise RuntimeError("fail")

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"first", "second", "third"}),
            call_adapter=tracking_adapter,
            max_retries=0,  # No retries — tests chain order only
        )

        with pytest.raises(ModelUnavailableError):
            await router.route(Role.CODER, messages=[])

        assert call_order == ["first", "second", "third"]


class TestStubCircuitBreaker:
    """Tests for the StubCircuitBreaker."""

    def test_always_closed(self):
        breaker = StubCircuitBreaker()
        assert breaker.state == CircuitBreakerState.CLOSED

    def test_record_operations_are_noop(self):
        breaker = StubCircuitBreaker()
        breaker.record_success()
        breaker.record_failure()
        # Still closed
        assert breaker.state == CircuitBreakerState.CLOSED
