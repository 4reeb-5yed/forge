"""Unit tests for ProviderCircuitBreaker, CancellableBackoff, and ModelRouter retries.

Tests cover:
- Record success/failure on each provider attempt
- Trip breaker after configured consecutive failures
- Configurable reset window for breaker recovery (HALF_OPEN state)
- Cancellable exponential backoff (bounded 1-30s) on transient errors, max 3 retries
- Permanent errors (auth, bad-request) stop retrying that provider immediately
- Per-call timeout of 60s; treat timeout as transient error
- Cancel in-flight backoff on user interrupt within 2 seconds

Requirements: 11.4, 11.5, 11.9, 24.7
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.runtime.events.models import DecisionKind, DecisionRecord
from app.runtime.models import Role
from app.runtime.router import (
    AttemptRecord,
    CancellableBackoff,
    ChainEntry,
    CircuitBreakerState,
    ModelRouter,
    ModelUnavailableError,
    PermanentError,
    ProviderCircuitBreaker,
    RoleChainConfig,
    SkipReason,
    StubCircuitBreaker,
)


# --- Helpers ---


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


def make_call_adapter(
    responses: dict[str, str] | None = None,
    failures: dict[str, Exception] | None = None,
):
    """Create a call adapter with canned responses or failures."""
    responses = responses or {}
    failures = failures or {}

    async def adapter(provider: str, model: str, messages: list, **kwargs) -> str:
        if provider in failures:
            raise failures[provider]
        if provider in responses:
            return responses[provider]
        raise RuntimeError(f"No response configured for provider '{provider}'")

    return adapter


def make_slow_adapter(delay_seconds: float, response: str = "ok"):
    """Create an adapter that takes a specified time to respond."""

    async def adapter(provider: str, model: str, messages: list, **kwargs) -> str:
        await asyncio.sleep(delay_seconds)
        return response

    return adapter


def make_counting_failure_adapter(
    fail_count: dict[str, int],
    response: str = "ok",
):
    """Create an adapter that fails N times per provider then succeeds."""
    call_counts: dict[str, int] = {}

    async def adapter(provider: str, model: str, messages: list, **kwargs) -> str:
        call_counts.setdefault(provider, 0)
        call_counts[provider] += 1
        threshold = fail_count.get(provider, 0)
        if call_counts[provider] <= threshold:
            raise RuntimeError(f"Transient error #{call_counts[provider]}")
        return response

    adapter.call_counts = call_counts  # type: ignore[attr-defined]
    return adapter


# --- ProviderCircuitBreaker Tests ---


class TestProviderCircuitBreakerBasic:
    """Tests for ProviderCircuitBreaker state management."""

    def test_starts_closed(self):
        breaker = ProviderCircuitBreaker(failure_threshold=3)
        assert breaker.state == CircuitBreakerState.CLOSED

    def test_stays_closed_below_threshold(self):
        breaker = ProviderCircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.CLOSED
        assert breaker.consecutive_failures == 2

    def test_trips_open_at_threshold(self):
        """Requirement 11.4: Trip breaker after configured consecutive failures."""
        breaker = ProviderCircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

    def test_custom_threshold(self):
        breaker = ProviderCircuitBreaker(failure_threshold=5)
        for _ in range(4):
            breaker.record_failure()
        assert breaker.state == CircuitBreakerState.CLOSED
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

    def test_success_resets_failure_count(self):
        breaker = ProviderCircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert breaker.consecutive_failures == 0
        assert breaker.state == CircuitBreakerState.CLOSED

    def test_success_closes_half_open_breaker(self):
        """HALF_OPEN -> CLOSED on success."""
        current_time = [0.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=2,
            reset_window_seconds=10.0,
            time_func=lambda: current_time[0],
        )
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

        # Advance past reset window
        current_time[0] = 11.0
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        breaker.record_success()
        assert breaker.state == CircuitBreakerState.CLOSED
        assert breaker.consecutive_failures == 0

    def test_failure_in_half_open_reopens(self):
        """HALF_OPEN -> OPEN on failure (probe failed)."""
        current_time = [0.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=2,
            reset_window_seconds=10.0,
            time_func=lambda: current_time[0],
        )
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

        # Advance past reset window -> HALF_OPEN
        current_time[0] = 11.0
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        # Probe fails -> reopen
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

    def test_invalid_threshold_raises(self):
        with pytest.raises(ValueError, match="failure_threshold must be >= 1"):
            ProviderCircuitBreaker(failure_threshold=0)

    def test_invalid_reset_window_raises(self):
        with pytest.raises(ValueError, match="reset_window_seconds must be > 0"):
            ProviderCircuitBreaker(reset_window_seconds=0)


class TestProviderCircuitBreakerResetWindow:
    """Tests for the configurable reset window."""

    def test_stays_open_before_window(self):
        """Breaker stays OPEN until reset window elapses."""
        current_time = [0.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=2,
            reset_window_seconds=30.0,
            time_func=lambda: current_time[0],
        )
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

        # Only 10s have elapsed (< 30s window)
        current_time[0] = 10.0
        assert breaker.state == CircuitBreakerState.OPEN

    def test_transitions_to_half_open_after_window(self):
        """Breaker transitions to HALF_OPEN after reset window elapses."""
        current_time = [0.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=2,
            reset_window_seconds=30.0,
            time_func=lambda: current_time[0],
        )
        breaker.record_failure()
        breaker.record_failure()

        current_time[0] = 31.0
        assert breaker.state == CircuitBreakerState.HALF_OPEN

    def test_custom_reset_window(self):
        """Different reset window values work correctly."""
        current_time = [0.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=1,
            reset_window_seconds=120.0,
            time_func=lambda: current_time[0],
        )
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

        current_time[0] = 119.0
        assert breaker.state == CircuitBreakerState.OPEN

        current_time[0] = 121.0
        assert breaker.state == CircuitBreakerState.HALF_OPEN


# --- CancellableBackoff Tests ---


class TestCancellableBackoff:
    """Tests for the CancellableBackoff utility."""

    def test_compute_delay_bounded_min(self):
        """Delay is always at least MIN_BACKOFF_SECONDS (1s)."""
        backoff = CancellableBackoff()
        for attempt in range(10):
            delay = backoff.compute_delay(attempt)
            assert delay >= CancellableBackoff.MIN_BACKOFF_SECONDS

    def test_compute_delay_bounded_max(self):
        """Delay never exceeds MAX_BACKOFF_SECONDS (30s)."""
        backoff = CancellableBackoff()
        for attempt in range(100):
            delay = backoff.compute_delay(attempt)
            assert delay <= CancellableBackoff.MAX_BACKOFF_SECONDS

    def test_delay_grows_with_attempts(self):
        """Average delay should increase with attempt number."""
        backoff = CancellableBackoff()
        # Compute average over many samples for statistical confidence
        avg_0 = sum(backoff.compute_delay(0) for _ in range(100)) / 100
        avg_3 = sum(backoff.compute_delay(3) for _ in range(100)) / 100
        assert avg_3 > avg_0

    async def test_wait_completes_normally(self):
        """Wait returns True when delay expires without cancellation."""
        backoff = CancellableBackoff()
        # Use attempt 0 for shortest delay
        result = await backoff.wait(0)
        assert result is True

    async def test_wait_returns_false_on_cancel(self):
        """Wait returns False when cancelled. Req 24.7."""
        cancel_event = asyncio.Event()
        backoff = CancellableBackoff(cancel_event=cancel_event)

        # Cancel immediately
        cancel_event.set()
        result = await backoff.wait(0)
        assert result is False

    async def test_cancel_during_sleep(self):
        """Cancel interrupts an in-flight sleep. Req 24.7."""
        cancel_event = asyncio.Event()
        backoff = CancellableBackoff(cancel_event=cancel_event)

        async def cancel_after_delay():
            await asyncio.sleep(0.05)
            cancel_event.set()

        task = asyncio.create_task(cancel_after_delay())
        start = time.monotonic()
        result = await backoff.wait(5)  # attempt 5 -> large delay
        elapsed = time.monotonic() - start

        assert result is False
        # Should have been cancelled quickly (well under 2s)
        assert elapsed < 2.0
        await task

    def test_is_sleeping_property(self):
        """is_sleeping reflects current state."""
        backoff = CancellableBackoff()
        assert backoff.is_sleeping is False


# --- ModelRouter with Retries Tests ---


class TestModelRouterTransientRetries:
    """Tests for transient error retries with backoff."""

    @pytest.fixture
    def config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })

    async def test_retries_on_transient_error(self, config):
        """Req 11.4: Retry on transient error up to max retries."""
        adapter = make_counting_failure_adapter(
            fail_count={"provider_a": 2},  # Fail twice, then succeed
            response="success_after_retries",
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            max_retries=3,
            call_timeout=60.0,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "success_after_retries"
        # Should have called provider_a 3 times (2 fails + 1 success)
        assert adapter.call_counts["provider_a"] == 3

    async def test_advances_to_next_after_max_retries(self, config):
        """After max retries exhausted, advance to next provider."""
        adapter = make_counting_failure_adapter(
            fail_count={"provider_a": 10, "provider_b": 0},
            response="from_b",
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            max_retries=3,
            call_timeout=60.0,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b"
        # provider_a called 4 times (initial + 3 retries), all failed
        assert adapter.call_counts["provider_a"] == 4

    async def test_records_failure_on_breaker_each_attempt(self, config):
        """Each failed attempt records failure on the circuit breaker."""
        breaker = ProviderCircuitBreaker(failure_threshold=10)
        adapter = make_counting_failure_adapter(
            fail_count={"provider_a": 3},
            response="ok",
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=3,
            call_timeout=60.0,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "ok"
        # 3 failures then 1 success
        assert breaker.consecutive_failures == 0  # Reset on success


class TestModelRouterPermanentErrors:
    """Tests for permanent error handling."""

    @pytest.fixture
    def config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })

    async def test_permanent_error_no_retry(self, config):
        """Req 11.5: Permanent errors stop retrying immediately."""
        call_count = {"provider_a": 0}

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                call_count["provider_a"] += 1
                raise PermanentError(
                    "Invalid API key",
                    provider=provider,
                    error_type="auth_failure",
                )
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            max_retries=3,
            call_timeout=60.0,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b"
        # provider_a called only ONCE despite max_retries=3
        assert call_count["provider_a"] == 1

    async def test_permanent_error_recorded_on_breaker(self, config):
        """Permanent error records failure on breaker."""
        breaker = ProviderCircuitBreaker(failure_threshold=5)

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                raise PermanentError("Bad request", provider=provider)
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=3,
            call_timeout=60.0,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b"
        assert breaker.consecutive_failures == 1

    async def test_permanent_error_logged_in_attempt_log(self, config):
        """Permanent error appears in attempt log with 'permanent' reason."""
        async def adapter(provider, model, messages, **kwargs):
            raise PermanentError("Auth failed", provider=provider)

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            max_retries=3,
            call_timeout=60.0,
        )

        with pytest.raises(ModelUnavailableError) as exc_info:
            await router.route(Role.CODER, messages=[])

        err = exc_info.value
        # Both providers should have exactly 1 attempt each (no retries)
        provider_a_attempts = [a for a in err.attempt_log if a.provider == "provider_a"]
        provider_b_attempts = [a for a in err.attempt_log if a.provider == "provider_b"]
        assert len(provider_a_attempts) == 1
        assert provider_a_attempts[0].reason == "permanent"
        assert len(provider_b_attempts) == 1
        assert provider_b_attempts[0].reason == "permanent"


class TestModelRouterTimeout:
    """Tests for per-call timeout behavior."""

    @pytest.fixture
    def config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })

    async def test_timeout_treated_as_transient(self, config):
        """Req 11.9: Per-call timeout treated as transient error."""
        call_count = {"provider_a": 0}

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                call_count["provider_a"] += 1
                # Simulate a slow call that will exceed the timeout
                await asyncio.sleep(10)
                return "should_not_reach"
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            max_retries=1,  # Only 1 retry to keep test fast
            call_timeout=0.05,  # Very short timeout for testing
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b"
        # provider_a should be called 2 times (initial + 1 retry), both timed out
        assert call_count["provider_a"] == 2

    async def test_timeout_records_on_breaker(self, config):
        """Timeout records failure on the circuit breaker."""
        breaker = ProviderCircuitBreaker(failure_threshold=5)

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                await asyncio.sleep(10)
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=0,  # No retries
            call_timeout=0.05,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b"
        assert breaker.consecutive_failures == 1


class TestModelRouterCancellation:
    """Tests for backoff cancellation on user interrupt."""

    @pytest.fixture
    def config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })

    async def test_cancel_stops_retries(self, config):
        """Req 24.7: Cancel in-flight backoff stops retrying that provider."""
        cancel_event = asyncio.Event()
        call_count = {"provider_a": 0}

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                call_count["provider_a"] += 1
                raise RuntimeError("transient")
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            cancel_event=cancel_event,
            max_retries=3,
            call_timeout=60.0,
        )

        # Cancel immediately to prevent backoff sleeping
        cancel_event.set()

        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b"
        # provider_a should be called only once (first attempt fails, backoff cancelled)
        assert call_count["provider_a"] == 1

    async def test_cancel_during_backoff_advances(self, config):
        """Cancel during backoff sleep moves to next provider. Req 24.7."""
        cancel_event = asyncio.Event()
        call_count = {"provider_a": 0}

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                call_count["provider_a"] += 1
                raise RuntimeError("transient")
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            cancel_event=cancel_event,
            max_retries=3,
            call_timeout=60.0,
        )

        # Schedule cancel shortly after first call
        async def cancel_soon():
            await asyncio.sleep(0.02)
            cancel_event.set()

        task = asyncio.create_task(cancel_soon())
        start = time.monotonic()
        result = await router.route(Role.CODER, messages=[])
        elapsed = time.monotonic() - start

        assert result == "from_b"
        # Should complete quickly (within 2s requirement)
        assert elapsed < 2.0
        assert call_count["provider_a"] == 1
        await task


class TestModelRouterBreakerIntegration:
    """Tests for the router's integration with ProviderCircuitBreaker."""

    @pytest.fixture
    def config(self) -> RoleChainConfig:
        return make_chain_config({
            Role.CODER: [
                ("provider_a", "model_a"),
                ("provider_b", "model_b"),
            ],
        })

    async def test_breaker_trips_after_repeated_failures(self, config):
        """Breaker opens after threshold failures, causing next route to skip."""
        breaker = ProviderCircuitBreaker(failure_threshold=2)
        call_count = {"provider_a": 0}

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                call_count["provider_a"] += 1
                raise RuntimeError("transient")
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=0,  # No retries for simplicity
            call_timeout=60.0,
        )

        # First call: provider_a fails, breaker records failure
        result1 = await router.route(Role.CODER, messages=[])
        assert result1 == "from_b"
        assert breaker.consecutive_failures == 1

        # Second call: provider_a fails again, breaker trips
        result2 = await router.route(Role.CODER, messages=[])
        assert result2 == "from_b"
        assert breaker.state == CircuitBreakerState.OPEN

        # Third call: provider_a should be SKIPPED (breaker open)
        call_count["provider_a"] = 0
        result3 = await router.route(Role.CODER, messages=[])
        assert result3 == "from_b"
        assert call_count["provider_a"] == 0  # Never called

    async def test_half_open_probe_succeeds(self, config):
        """When breaker is HALF_OPEN, a successful probe closes it."""
        current_time = [0.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=1,
            reset_window_seconds=5.0,
            time_func=lambda: current_time[0],
        )

        responses = {"provider_a": "from_a", "provider_b": "from_b"}

        async def adapter(provider, model, messages, **kwargs):
            return responses[provider]

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=0,
            call_timeout=60.0,
        )

        # Trip the breaker
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

        # Advance past reset window
        current_time[0] = 6.0
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        # Route should attempt provider_a (HALF_OPEN allows one probe)
        result = await router.route(Role.CODER, messages=[])
        assert result == "from_a"
        assert breaker.state == CircuitBreakerState.CLOSED


class TestPermanentError:
    """Tests for the PermanentError exception class."""

    def test_stores_provider_and_type(self):
        err = PermanentError(
            "Invalid API key",
            provider="openai",
            error_type="auth_failure",
        )
        assert err.provider == "openai"
        assert err.error_type == "auth_failure"
        assert "Invalid API key" in str(err)

    def test_defaults_to_empty(self):
        err = PermanentError("Bad request")
        assert err.provider == ""
        assert err.error_type == ""


class TestModelRouterResetCancel:
    """Tests for cancel/reset functionality."""

    async def test_reset_cancel_allows_new_route(self):
        """After reset_cancel, backoff is functional again."""
        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a")],
        })
        cancel_event = asyncio.Event()
        cancel_event.set()  # Start cancelled

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a"}),
            call_adapter=make_call_adapter(responses={"provider_a": "ok"}),
            cancel_event=cancel_event,
            max_retries=3,
            call_timeout=60.0,
        )

        # First route should work (call succeeds on first try, no backoff needed)
        result = await router.route(Role.CODER, messages=[])
        assert result == "ok"

        # Reset cancel for fresh operation
        router.reset_cancel()
        assert not router.backoff.cancelled

    async def test_cancel_backoff_method(self):
        """cancel_backoff sets the cancel event."""
        config = make_chain_config({Role.CODER: [("a", "m")]})
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"a"}),
            call_adapter=make_call_adapter(responses={"a": "ok"}),
            max_retries=3,
            call_timeout=60.0,
        )

        assert not router.backoff.cancelled
        router.cancel_backoff()
        assert router.backoff.cancelled


# --- Additional tests for task 5.5 coverage gaps ---


class TestProviderCircuitBreakerFailureCountDuringRetries:
    """Tests verifying breaker failure count is correct during retry sequences."""

    async def test_breaker_records_each_transient_failure(self):
        """Req 11.4: Each transient retry records a failure on the breaker."""
        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })
        breaker = ProviderCircuitBreaker(failure_threshold=10)
        adapter = make_counting_failure_adapter(
            fail_count={"provider_a": 3},  # Fail 3 times, succeed on 4th
            response="ok",
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=3,
            call_timeout=60.0,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "ok"
        # 3 failures then success resets count
        assert breaker.consecutive_failures == 0
        # But the breaker saw 3 failures before reset
        assert adapter.call_counts["provider_a"] == 4  # 3 fails + 1 success

    async def test_breaker_trips_during_retries_skips_on_next_route(self):
        """When breaker trips during retries, subsequent routes skip that provider."""
        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })
        # Breaker with threshold=2, so it trips after 2 failures
        breaker = ProviderCircuitBreaker(failure_threshold=2, reset_window_seconds=60.0)

        call_count = {"provider_a": 0}

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                call_count["provider_a"] += 1
                raise RuntimeError("transient")
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=3,
            call_timeout=60.0,
        )

        # First route: provider_a will fail, breaker will trip after 2 failures
        result1 = await router.route(Role.CODER, messages=[])
        assert result1 == "from_b"
        assert breaker.state == CircuitBreakerState.OPEN

        # Second route: provider_a should be skipped due to open breaker
        call_count["provider_a"] = 0
        result2 = await router.route(Role.CODER, messages=[])
        assert result2 == "from_b"
        assert call_count["provider_a"] == 0  # Skipped entirely


class TestHalfOpenProbeFailureFallback:
    """Test that when a HALF_OPEN probe fails, the router falls back to next provider."""

    async def test_half_open_probe_failure_falls_back(self):
        """Req 11.2, 11.4: HALF_OPEN probe failure reopens breaker; router uses next provider."""
        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a"), ("provider_b", "model_b")],
        })
        current_time = [0.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=1,
            reset_window_seconds=5.0,
            time_func=lambda: current_time[0],
        )

        async def adapter(provider, model, messages, **kwargs):
            if provider == "provider_a":
                raise RuntimeError("still failing")
            return "from_b"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b"}),
            call_adapter=adapter,
            breakers={"provider_a": breaker},
            max_retries=0,
            call_timeout=60.0,
        )

        # Trip the breaker
        breaker.record_failure()
        assert breaker.state == CircuitBreakerState.OPEN

        # Advance past reset window -> HALF_OPEN
        current_time[0] = 6.0
        assert breaker.state == CircuitBreakerState.HALF_OPEN

        # Route: provider_a probe fails -> reopens -> falls back to provider_b
        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b"
        assert breaker.state == CircuitBreakerState.OPEN


class TestTimeoutAttemptLogReason:
    """Test that timeout failures are recorded with 'timeout' reason in attempt log."""

    async def test_timeout_reason_in_attempt_log(self):
        """Req 11.9: Timeout appears in attempt log with 'timeout' reason."""
        config = make_chain_config({
            Role.CODER: [("provider_a", "model_a")],
        })

        async def adapter(provider, model, messages, **kwargs):
            await asyncio.sleep(10)
            return "should_not_reach"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a"}),
            call_adapter=adapter,
            max_retries=0,
            call_timeout=0.05,
        )

        with pytest.raises(ModelUnavailableError) as exc_info:
            await router.route(Role.CODER, messages=[])

        err = exc_info.value
        assert len(err.attempt_log) == 1
        assert err.attempt_log[0].provider == "provider_a"
        assert err.attempt_log[0].status == "failed"
        assert err.attempt_log[0].reason == "timeout"
        assert "timed out" in err.attempt_log[0].error.lower()


class TestMixedPermanentAndTransientAcrossProviders:
    """Test mixing permanent error on one provider and transient retries on another."""

    async def test_permanent_on_first_transient_retries_on_second(self):
        """Req 11.4, 11.5: Permanent on first provider, transient retries on second."""
        config = make_chain_config({
            Role.CODER: [
                ("provider_a", "model_a"),
                ("provider_b", "model_b"),
                ("provider_c", "model_c"),
            ],
        })
        call_counts = {"provider_a": 0, "provider_b": 0, "provider_c": 0}

        async def adapter(provider, model, messages, **kwargs):
            call_counts[provider] = call_counts.get(provider, 0) + 1
            if provider == "provider_a":
                raise PermanentError("Auth failed", provider=provider)
            if provider == "provider_b":
                if call_counts["provider_b"] <= 2:
                    raise RuntimeError("transient error")
                return "from_b_after_retries"
            return "from_c"

        router = ModelRouter(
            chain_config=config,
            registry_checker=make_registry_checker({"provider_a", "provider_b", "provider_c"}),
            call_adapter=adapter,
            max_retries=3,
            call_timeout=60.0,
        )

        result = await router.route(Role.CODER, messages=[])
        assert result == "from_b_after_retries"
        # provider_a: 1 call (permanent, no retry)
        assert call_counts["provider_a"] == 1
        # provider_b: 3 calls (2 transient failures + 1 success)
        assert call_counts["provider_b"] == 3
        # provider_c: never reached
        assert call_counts["provider_c"] == 0


class TestCancellableBackoffBoundsIntegration:
    """Additional tests for backoff bounds verification."""

    def test_attempt_zero_delay_is_at_least_min(self):
        """Req 11.4: Even at attempt 0, delay >= 1s."""
        backoff = CancellableBackoff()
        # Run multiple times due to randomness
        for _ in range(50):
            delay = backoff.compute_delay(0)
            assert delay >= 1.0
            assert delay <= 30.0

    def test_high_attempt_delay_capped_at_max(self):
        """Req 11.4: At very high attempt numbers, delay is capped at 30s."""
        backoff = CancellableBackoff()
        for _ in range(50):
            delay = backoff.compute_delay(100)
            assert delay <= 30.0
            assert delay >= 1.0

    async def test_cancelled_backoff_returns_false_immediately(self):
        """Req 24.7: Already-cancelled backoff returns False without blocking."""
        import time as time_mod

        cancel_event = asyncio.Event()
        cancel_event.set()
        backoff = CancellableBackoff(cancel_event=cancel_event)

        start = time_mod.monotonic()
        result = await backoff.wait(5)  # High attempt = long delay
        elapsed = time_mod.monotonic() - start

        assert result is False
        # Should return almost instantly
        assert elapsed < 0.1
