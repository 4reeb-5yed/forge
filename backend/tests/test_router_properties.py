"""Property-based tests for Model Router routing soundness and fallback monotonicity.

**Property 1: Routing soundness** — route() returns the completion from the first
provider that is present in Registry, breaker-closed, and succeeds; never returns
from unavailable or breaker-open provider.

**Property 7: Router fallback monotonicity** — route() tries providers strictly in
chain order, never retries after PermanentError, raises ModelUnavailableError only
after exhausting chain.

**Validates: Requirements 11.1, 11.2, 11.3, 11.6, 11.7**

Uses Hypothesis to generate arbitrary chain configs (roles with 1–5 providers),
arbitrary registry states (which providers are available), arbitrary breaker states
(open/closed/half-open), and arbitrary call outcomes (success/transient failure/
permanent failure).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.runtime.models import Role
from app.runtime.router import (
    ChainEntry,
    CircuitBreakerState,
    ModelRouter,
    ModelUnavailableError,
    PermanentError,
    ProviderCircuitBreaker,
    RoleChainConfig,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Provider names: short ASCII identifiers
provider_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"),
    min_size=3,
    max_size=10,
)

# Model names: short identifiers
model_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_"),
    min_size=3,
    max_size=15,
)

# Roles: sample from real roles
roles = st.sampled_from(list(Role))


class CallOutcome(str, Enum):
    """Possible outcomes for a simulated provider call."""

    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient"
    PERMANENT_FAILURE = "permanent"


# Strategy for call outcomes
call_outcomes = st.sampled_from(list(CallOutcome))

# Breaker state (only OPEN or CLOSED for simplicity — HALF_OPEN treated as CLOSED for routing)
breaker_states = st.sampled_from([CircuitBreakerState.CLOSED, CircuitBreakerState.OPEN])


@st.composite
def chain_entry_strategy(draw: st.DrawFn) -> ChainEntry:
    """Generate a single chain entry with unique provider+model."""
    provider = draw(provider_names)
    model = draw(model_names)
    return ChainEntry(provider=provider, model=model)


@st.composite
def chain_config_strategy(draw: st.DrawFn) -> tuple[Role, list[ChainEntry]]:
    """Generate a role with 1–5 providers in its chain.

    Ensures provider names are unique within the chain.
    """
    role = draw(roles)
    chain_size = draw(st.integers(min_value=1, max_value=5))
    # Generate unique provider names for this chain
    providers = draw(
        st.lists(
            provider_names,
            min_size=chain_size,
            max_size=chain_size,
            unique=True,
        )
    )
    models = draw(
        st.lists(
            model_names,
            min_size=chain_size,
            max_size=chain_size,
        )
    )
    chain = [ChainEntry(provider=p, model=m) for p, m in zip(providers, models)]
    return (role, chain)


@dataclass
class ProviderSetup:
    """Complete setup for a property test scenario."""

    role: Role
    chain: list[ChainEntry]
    registry_available: set[str]  # providers present in registry
    breaker_states: dict[str, CircuitBreakerState]  # provider -> breaker state
    call_outcomes: dict[str, CallOutcome]  # provider -> what happens on call


@st.composite
def routing_scenario(draw: st.DrawFn) -> ProviderSetup:
    """Generate a complete routing scenario with chain, registry, breakers, outcomes."""
    role, chain = draw(chain_config_strategy())
    provider_list = [entry.provider for entry in chain]

    # Each provider independently may or may not be in registry
    registry_available = set()
    for p in provider_list:
        if draw(st.booleans()):
            registry_available.add(p)

    # Each provider independently has a breaker state
    breaker_map = {}
    for p in provider_list:
        breaker_map[p] = draw(breaker_states)

    # Each provider that could be attempted has a call outcome
    outcome_map = {}
    for p in provider_list:
        outcome_map[p] = draw(call_outcomes)

    return ProviderSetup(
        role=role,
        chain=chain,
        registry_available=registry_available,
        breaker_states=breaker_map,
        call_outcomes=outcome_map,
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_router(setup: ProviderSetup) -> tuple[ModelRouter, list[str]]:
    """Build a ModelRouter from a ProviderSetup with tracked attempts.

    Returns the router and a list that will be populated with provider names
    in the order they were actually called (not skipped).
    """
    attempted_providers: list[str] = []

    # Registry checker: returns True if provider is in the available set
    def registry_checker(provider: str) -> bool:
        return provider in setup.registry_available

    # Call adapter: simulates the outcome for each provider
    async def call_adapter(provider: str, model: str, messages: list, **kwargs) -> str:
        attempted_providers.append(provider)
        outcome = setup.call_outcomes.get(provider, CallOutcome.TRANSIENT_FAILURE)
        if outcome == CallOutcome.SUCCESS:
            return f"completion_from_{provider}"
        elif outcome == CallOutcome.PERMANENT_FAILURE:
            raise PermanentError(
                f"Permanent error from {provider}",
                provider=provider,
                error_type="auth_failure",
            )
        else:
            # Transient failure
            raise RuntimeError(f"Transient error from {provider}")

    # Build circuit breakers with the specified states
    breakers: dict[str, ProviderCircuitBreaker] = {}
    for provider, state in setup.breaker_states.items():
        breaker = ProviderCircuitBreaker(failure_threshold=5, reset_window_seconds=60.0)
        if state == CircuitBreakerState.OPEN:
            # Trip the breaker by recording enough failures
            for _ in range(5):
                breaker.record_failure()
            assert breaker.state == CircuitBreakerState.OPEN
        breakers[provider] = breaker

    chain_config = RoleChainConfig(chains={setup.role: setup.chain})

    router = ModelRouter(
        chain_config=chain_config,
        registry_checker=registry_checker,
        call_adapter=call_adapter,
        breakers=breakers,
        max_retries=0,  # No retries — focus on chain ordering
        call_timeout=5.0,
    )

    return router, attempted_providers


def _is_eligible(setup: ProviderSetup, provider: str) -> bool:
    """Check if a provider is eligible for an attempt (in registry + breaker not open)."""
    return (
        provider in setup.registry_available
        and setup.breaker_states.get(provider) != CircuitBreakerState.OPEN
    )


def _expected_first_success(setup: ProviderSetup) -> str | None:
    """Determine which provider should succeed first, walking chain in order.

    Returns the provider name or None if no provider will succeed.
    """
    for entry in setup.chain:
        if not _is_eligible(setup, entry.provider):
            continue
        outcome = setup.call_outcomes.get(entry.provider, CallOutcome.TRANSIENT_FAILURE)
        if outcome == CallOutcome.SUCCESS:
            return entry.provider
        # PermanentError or TransientError (with max_retries=0) -> advance to next
    return None


# ---------------------------------------------------------------------------
# Property 1: Routing Soundness
# ---------------------------------------------------------------------------


class TestRoutingSoundnessProperty:
    """Property 1: Routing soundness — route() returns the completion from the
    first provider that is present in Registry, breaker-closed, and succeeds;
    never returns from unavailable or breaker-open provider.

    **Validates: Requirements 11.1, 11.2, 11.3, 11.6, 11.7**
    """

    @given(setup=routing_scenario())
    @settings(max_examples=500)
    def test_route_returns_first_eligible_success(self, setup: ProviderSetup):
        """route() returns the completion from the first eligible successful
        provider in chain order. If no provider succeeds, raises
        ModelUnavailableError.

        Core invariant: The returned completion always comes from the first
        provider in chain order that is (a) in registry, (b) breaker not OPEN,
        and (c) returns success on call.
        """
        router, attempted_providers = _build_router(setup)
        expected_winner = _expected_first_success(setup)

        loop = asyncio.new_event_loop()
        try:
            if expected_winner is not None:
                result = loop.run_until_complete(
                    router.route(setup.role, [{"role": "user", "content": "test"}])
                )
                # Result must come from the expected winner
                assert result == f"completion_from_{expected_winner}", (
                    f"Expected completion from '{expected_winner}' but got '{result}'. "
                    f"Chain: {[e.provider for e in setup.chain]}, "
                    f"Registry: {setup.registry_available}, "
                    f"Breakers: {setup.breaker_states}, "
                    f"Outcomes: {setup.call_outcomes}"
                )
            else:
                # All providers exhausted — must raise ModelUnavailableError
                with pytest.raises(ModelUnavailableError) as exc_info:
                    loop.run_until_complete(
                        router.route(setup.role, [{"role": "user", "content": "test"}])
                    )
                assert exc_info.value.role == setup.role
        finally:
            loop.close()

    @given(setup=routing_scenario())
    @settings(max_examples=500)
    def test_never_returns_from_ineligible_provider(self, setup: ProviderSetup):
        """route() NEVER returns a completion from a provider that is not in
        registry or whose breaker is open. This is the negative safety invariant.
        """
        router, attempted_providers = _build_router(setup)

        loop = asyncio.new_event_loop()
        try:
            try:
                result = loop.run_until_complete(
                    router.route(setup.role, [{"role": "user", "content": "test"}])
                )
                # If we got a result, it must be from an eligible provider
                # Parse the provider from the completion string
                assert result.startswith("completion_from_")
                winning_provider = result[len("completion_from_"):]

                # Winner must be in registry
                assert winning_provider in setup.registry_available, (
                    f"Got completion from '{winning_provider}' which is NOT in registry. "
                    f"Registry: {setup.registry_available}"
                )
                # Winner must have breaker not open
                assert setup.breaker_states.get(winning_provider) != CircuitBreakerState.OPEN, (
                    f"Got completion from '{winning_provider}' whose breaker is OPEN."
                )
            except ModelUnavailableError:
                # Acceptable — all providers exhausted
                pass
        finally:
            loop.close()

    @given(setup=routing_scenario())
    @settings(max_examples=500)
    def test_ineligible_providers_never_attempted(self, setup: ProviderSetup):
        """Providers not in registry or with open breakers are never called
        (the call adapter is never invoked for them).
        """
        router, attempted_providers = _build_router(setup)

        loop = asyncio.new_event_loop()
        try:
            try:
                loop.run_until_complete(
                    router.route(setup.role, [{"role": "user", "content": "test"}])
                )
            except ModelUnavailableError:
                pass

            # Verify no ineligible provider was attempted
            for provider in attempted_providers:
                assert provider in setup.registry_available, (
                    f"Provider '{provider}' was attempted but is NOT in registry."
                )
                assert setup.breaker_states.get(provider) != CircuitBreakerState.OPEN, (
                    f"Provider '{provider}' was attempted but its breaker is OPEN."
                )
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Property 7: Router Fallback Monotonicity
# ---------------------------------------------------------------------------


class TestRouterFallbackMonotonicity:
    """Property 7: Router fallback monotonicity — route() tries providers strictly
    in chain order, never retries after PermanentError, raises ModelUnavailableError
    only after exhausting chain.

    **Validates: Requirements 11.1, 11.2, 11.3, 11.6, 11.7**
    """

    @given(setup=routing_scenario())
    @settings(max_examples=500)
    def test_providers_attempted_in_chain_order(self, setup: ProviderSetup):
        """Providers that are actually attempted (call adapter invoked) are
        attempted in the same order they appear in the chain configuration.
        No provider is attempted out of order.
        """
        router, attempted_providers = _build_router(setup)

        loop = asyncio.new_event_loop()
        try:
            try:
                loop.run_until_complete(
                    router.route(setup.role, [{"role": "user", "content": "test"}])
                )
            except ModelUnavailableError:
                pass

            # Build the expected order: eligible providers in chain order
            chain_order = [entry.provider for entry in setup.chain]

            # attempted_providers must be a subsequence of chain_order
            # (they appear in the same relative order as the chain)
            if attempted_providers:
                prev_idx = -1
                for attempted in attempted_providers:
                    idx = chain_order.index(attempted)
                    assert idx > prev_idx, (
                        f"Provider '{attempted}' at chain index {idx} was attempted "
                        f"after a provider at index {prev_idx}. "
                        f"Chain order: {chain_order}, "
                        f"Attempted order: {attempted_providers}"
                    )
                    prev_idx = idx
        finally:
            loop.close()

    @given(setup=routing_scenario())
    @settings(max_examples=500)
    def test_no_retry_after_permanent_error(self, setup: ProviderSetup):
        """After PermanentError from a provider, that provider is never retried.
        Each provider appears at most once in the attempted list (with max_retries=0).
        """
        router, attempted_providers = _build_router(setup)

        loop = asyncio.new_event_loop()
        try:
            try:
                loop.run_until_complete(
                    router.route(setup.role, [{"role": "user", "content": "test"}])
                )
            except ModelUnavailableError:
                pass

            # With max_retries=0, each provider should appear at most once
            # (PermanentError causes immediate advance; transient also advances
            # since there are no retries)
            seen = set()
            for provider in attempted_providers:
                assert provider not in seen, (
                    f"Provider '{provider}' was attempted more than once. "
                    f"Attempts: {attempted_providers}. "
                    f"With max_retries=0, no provider should be retried."
                )
                seen.add(provider)
        finally:
            loop.close()

    @given(setup=routing_scenario())
    @settings(max_examples=500)
    def test_model_unavailable_only_after_exhausting_chain(self, setup: ProviderSetup):
        """ModelUnavailableError is raised ONLY when no provider in the chain
        can serve the request. If any eligible provider would succeed, the
        router must return its completion instead of raising.
        """
        router, attempted_providers = _build_router(setup)
        expected_winner = _expected_first_success(setup)

        loop = asyncio.new_event_loop()
        try:
            if expected_winner is not None:
                # There exists a provider that should succeed — must NOT raise
                result = loop.run_until_complete(
                    router.route(setup.role, [{"role": "user", "content": "test"}])
                )
                assert result == f"completion_from_{expected_winner}"
            else:
                # No provider can succeed — MUST raise ModelUnavailableError
                with pytest.raises(ModelUnavailableError) as exc_info:
                    loop.run_until_complete(
                        router.route(setup.role, [{"role": "user", "content": "test"}])
                    )
                # The error must identify the role
                assert exc_info.value.role == setup.role
                # The attempt log must contain entries for every chain member
                logged_providers = {a.provider for a in exc_info.value.attempt_log}
                chain_providers = {entry.provider for entry in setup.chain}
                assert logged_providers == chain_providers, (
                    f"Attempt log providers {logged_providers} don't match "
                    f"chain providers {chain_providers}. "
                    f"All chain providers must appear in the attempt log."
                )
        finally:
            loop.close()

    @given(setup=routing_scenario())
    @settings(max_examples=500)
    def test_attempts_stop_at_first_success(self, setup: ProviderSetup):
        """Once a provider succeeds, no further providers are attempted.
        The router must short-circuit on first success.
        """
        router, attempted_providers = _build_router(setup)
        expected_winner = _expected_first_success(setup)

        loop = asyncio.new_event_loop()
        try:
            try:
                loop.run_until_complete(
                    router.route(setup.role, [{"role": "user", "content": "test"}])
                )
            except ModelUnavailableError:
                pass

            if expected_winner is not None:
                # The winning provider should be the last one attempted
                assert attempted_providers[-1] == expected_winner, (
                    f"Expected '{expected_winner}' to be the last attempted provider "
                    f"but attempted list is {attempted_providers}."
                )
                # No provider after the winner in chain order should be attempted
                chain_order = [entry.provider for entry in setup.chain]
                winner_idx = chain_order.index(expected_winner)
                providers_after_winner = set(chain_order[winner_idx + 1:])
                for attempted in attempted_providers:
                    if attempted in providers_after_winner:
                        pytest.fail(
                            f"Provider '{attempted}' was attempted after winner "
                            f"'{expected_winner}' (chain index {winner_idx}). "
                            f"Chain: {chain_order}, Attempted: {attempted_providers}"
                        )
        finally:
            loop.close()
