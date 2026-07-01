"""Model Router - role-based routing with fallback chain and circuit breaker.

Resolves a Role to a healthy provider+model using an ordered fallback chain,
a per-provider circuit breaker, and registry availability checks. The actual
model call is delegated to an async callable (adapter pattern).

Integrates with SessionBudget for pre-call estimation checks and post-call
token charging, and emits model.selected/model.fallback events for observability.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 12.2, 12.3, 12.4, 24.7
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol


from app.runtime.budget import BudgetExceededError, SessionBudget
from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType
from app.runtime.models import Role

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelUnavailableError(Exception):
    """Raised when all providers in the chain are exhausted for a Role.

    Per requirement 11.6: if every provider in the chain is skipped or exhausted,
    raise this error identifying the Role and the list of providers attempted.
    """

    def __init__(self, role: Role, attempt_log: list[AttemptRecord]) -> None:
        self.role = role
        self.attempt_log = attempt_log
        providers = [a.provider for a in attempt_log]
        super().__init__(
            f"Model unavailable for role '{role.value}': "
            f"all providers exhausted. Attempted: {providers}"
        )


class PermanentError(Exception):
    """Raised when a provider call fails with a permanent (non-retryable) error.

    Permanent errors include authentication failures, authorization failures,
    malformed requests, and unsupported model errors. These should NOT be retried
    and should cause the router to immediately advance to the next provider.

    Per requirement 11.5: on permanent error, stop retrying that provider
    immediately and advance to the next provider in the chain.
    """

    def __init__(self, message: str, *, provider: str = "", error_type: str = "") -> None:
        self.provider = provider
        self.error_type = error_type
        super().__init__(message)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class SkipReason(str, Enum):
    """Reason a provider was skipped without attempting a call."""

    NOT_IN_REGISTRY = "not_in_registry"
    BREAKER_OPEN = "breaker_open"


@dataclass
class AttemptRecord:
    """Record of a single provider attempt or skip in the routing chain.

    Used to build the attempt log for audit trail recording on exhaustion.
    """

    provider: str
    model: str
    status: str  # "success", "skipped", "failed"
    reason: str = ""
    error: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ChainEntry:
    """A single entry in a role's fallback chain: provider + model pair."""

    provider: str
    model: str


class CircuitBreakerState(str, Enum):
    """States of a per-provider circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Circuit Breaker Protocol and Implementations
# ---------------------------------------------------------------------------


class CircuitBreaker(Protocol):
    """Protocol for a per-provider circuit breaker.

    Defines the interface the ModelRouter depends on.
    """

    @property
    def state(self) -> CircuitBreakerState:
        """Current breaker state."""
        ...

    def record_success(self) -> None:
        """Record a successful call."""
        ...

    def record_failure(self) -> None:
        """Record a failed call."""
        ...


class StubCircuitBreaker:
    """A stub circuit breaker that is always closed (never trips).

    Used as default until ProviderCircuitBreaker is configured.
    """

    @property
    def state(self) -> CircuitBreakerState:
        return CircuitBreakerState.CLOSED

    def record_success(self) -> None:
        pass

    def record_failure(self) -> None:
        pass


class ProviderCircuitBreaker:
    """Full per-provider circuit breaker with configurable thresholds and reset window.

    State machine:
    - CLOSED: Normal operation. Track consecutive failures.
    - OPEN: Provider is considered down. Skip all attempts. After reset_window_seconds
      elapses, transition to HALF_OPEN.
    - HALF_OPEN: Allow exactly one probe attempt. On success -> CLOSED, on failure -> OPEN.

    Requirements:
        11.4 - Record success/failure, trip after consecutive failure threshold
        11.5 - Configurable reset window for breaker recovery
        24.7 - Cancellable backoff support (breaker state drives skip decisions)
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_window_seconds: float = 60.0,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the circuit breaker.

        Args:
            failure_threshold: Number of consecutive failures before tripping
                the breaker open. Must be >= 1.
            reset_window_seconds: Seconds after opening before allowing a
                half-open probe attempt. Must be > 0.
            time_func: Optional time function for testing (defaults to time.monotonic).
        """
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_window_seconds <= 0:
            raise ValueError("reset_window_seconds must be > 0")

        self._failure_threshold = failure_threshold
        self._reset_window_seconds = reset_window_seconds
        self._time_func = time_func or time.monotonic

        self._consecutive_failures: int = 0
        self._state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self._opened_at: float = 0.0

    @property
    def state(self) -> CircuitBreakerState:
        """Current breaker state, with automatic OPEN -> HALF_OPEN transition.

        If the breaker is OPEN and the reset window has elapsed, it
        automatically transitions to HALF_OPEN to allow a probe attempt.
        """
        if self._state == CircuitBreakerState.OPEN:
            elapsed = self._time_func() - self._opened_at
            if elapsed >= self._reset_window_seconds:
                self._state = CircuitBreakerState.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failures recorded."""
        return self._consecutive_failures

    @property
    def failure_threshold(self) -> int:
        """Configured failure threshold."""
        return self._failure_threshold

    @property
    def reset_window_seconds(self) -> float:
        """Configured reset window in seconds."""
        return self._reset_window_seconds

    def record_success(self) -> None:
        """Record a successful call.

        Resets consecutive failure count and transitions to CLOSED
        regardless of current state (CLOSED stays CLOSED, HALF_OPEN -> CLOSED).
        """
        self._consecutive_failures = 0
        self._state = CircuitBreakerState.CLOSED

    def record_failure(self) -> None:
        """Record a failed call.

        Increments consecutive failure count. If threshold is reached,
        transitions to OPEN. If already HALF_OPEN, immediately reopens.
        """
        self._consecutive_failures += 1

        if self._state == CircuitBreakerState.HALF_OPEN:
            # HALF_OPEN probe failed -> reopen immediately
            self._state = CircuitBreakerState.OPEN
            self._opened_at = self._time_func()
        elif self._consecutive_failures >= self._failure_threshold:
            # Threshold reached -> trip open
            self._state = CircuitBreakerState.OPEN
            self._opened_at = self._time_func()


# ---------------------------------------------------------------------------
# Backoff utility
# ---------------------------------------------------------------------------


class CancellableBackoff:
    """Cancellable exponential backoff with jitter for transient retries.

    Implements bounded exponential backoff (1-30 seconds) with full jitter.
    Supports cancellation via an asyncio Event, allowing user interrupts
    to cancel in-flight sleep within 2 seconds (Requirement 24.7).
    """

    # Bounds per requirement 11.4
    MIN_BACKOFF_SECONDS: float = 1.0
    MAX_BACKOFF_SECONDS: float = 30.0

    def __init__(self, *, cancel_event: asyncio.Event | None = None) -> None:
        """Initialize the backoff controller.

        Args:
            cancel_event: An asyncio.Event that, when set, cancels any
                in-flight backoff sleep immediately.
        """
        self._cancel_event = cancel_event or asyncio.Event()
        self._sleeping = False

    @property
    def is_sleeping(self) -> bool:
        """Whether a backoff sleep is currently in progress."""
        return self._sleeping

    def cancel(self) -> None:
        """Cancel any in-flight backoff sleep.

        Per requirement 24.7: cancel in-flight backoff on user interrupt
        within 2 seconds.
        """
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        """Whether the backoff has been cancelled."""
        return self._cancel_event.is_set()

    def compute_delay(self, attempt: int) -> float:
        """Compute the backoff delay for a given attempt number.

        Uses exponential backoff with full jitter, bounded between
        MIN_BACKOFF_SECONDS and MAX_BACKOFF_SECONDS.

        Args:
            attempt: The attempt number (0-based). First retry is attempt=0.

        Returns:
            Delay in seconds.
        """
        # Exponential base: 2^attempt seconds (starting at 1s)
        base_delay = min(
            2.0 ** attempt,
            self.MAX_BACKOFF_SECONDS,
        )
        # Apply full jitter: uniform random between min and base
        delay = random.uniform(self.MIN_BACKOFF_SECONDS, max(self.MIN_BACKOFF_SECONDS, base_delay))
        # Clamp to bounds
        return max(self.MIN_BACKOFF_SECONDS, min(delay, self.MAX_BACKOFF_SECONDS))

    async def wait(self, attempt: int) -> bool:
        """Wait for the computed backoff delay, or until cancelled.

        Args:
            attempt: The attempt number (0-based) to compute delay from.

        Returns:
            True if the wait completed normally, False if cancelled.
        """
        if self._cancel_event.is_set():
            return False

        delay = self.compute_delay(attempt)
        self._sleeping = True
        try:
            # Wait for either the delay to expire or cancellation
            try:
                await asyncio.wait_for(
                    self._cancel_event.wait(),
                    timeout=delay,
                )
                # If we get here, the event was set (cancelled)
                return False
            except asyncio.TimeoutError:
                # Timeout means the delay expired normally (not cancelled)
                return True
        finally:
            self._sleeping = False


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# Type alias for the model call adapter
# Takes (provider, model, messages, **kwargs) -> completion string
ModelCallAdapter = Callable[..., Awaitable[str]]

# Type alias for audit trail recording
AuditRecorder = Callable[[DecisionRecord], Awaitable[Any]]

# Type alias for checking provider availability in the registry
RegistryChecker = Callable[[str], bool]

# Type alias for the event emission callback
EventEmitter = Callable[[Event], Awaitable[Any]]

# Default estimated tokens when none specified
DEFAULT_ESTIMATED_TOKENS = 1000

# Default per-call timeout in seconds (Requirement 11.9)
DEFAULT_CALL_TIMEOUT_SECONDS: float = 60.0

# Default max retries per provider on transient errors (Requirement 11.4)
DEFAULT_MAX_RETRIES: int = 3


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RoleChainConfig:
    """Configuration mapping roles to ordered provider+model chains.

    Parsed from models.yaml. Each role has an ordered list of ChainEntry
    objects representing the fallback chain.
    """

    chains: dict[Role, list[ChainEntry]] = field(default_factory=dict)

    @staticmethod
    def from_yaml_dict(data: dict[str, Any]) -> RoleChainConfig:
        """Parse a RoleChainConfig from the 'roles' section of models.yaml.

        Args:
            data: The 'roles' dict from models.yaml, mapping role names
                  to objects with a 'chain' list of {provider, model} dicts.

        Returns:
            A RoleChainConfig with parsed chains.
        """
        chains: dict[Role, list[ChainEntry]] = {}
        roles_data = data.get("roles", data)

        for role_name, role_config in roles_data.items():
            try:
                role = Role(role_name)
            except ValueError:
                logger.warning("Unknown role '%s' in models.yaml, skipping", role_name)
                continue

            chain_data = role_config.get("chain", [])
            chain_entries = [
                ChainEntry(provider=entry["provider"], model=entry["model"])
                for entry in chain_data
            ]
            chains[role] = chain_entries

        return RoleChainConfig(chains=chains)


# ---------------------------------------------------------------------------
# Model Router
# ---------------------------------------------------------------------------


class ModelRouter:
    """Role-based model router with ordered fallback chain and circuit breaker retries.

    Resolves a Role to a provider+model by walking the configured chain in order,
    skipping providers that are not in the CapabilityRegistry or whose circuit
    breaker is open, and attempting a call on each remaining provider with
    configurable retries for transient errors.

    On success: returns the completion result from the first healthy provider.
    On exhaustion: raises ModelUnavailableError with the full attempt log and
    records the attempt in the AuditTrail.

    Requirements:
        11.1 - Attempt providers strictly in configured chain order
        11.2 - Skip providers not in Registry or with open circuit breaker
        11.3 - Return first successful completion, record success on breaker
        11.4 - Cancellable backoff (1-30s) on transient errors, max 3 retries
        11.5 - Permanent errors stop retrying that provider immediately
        11.6 - Raise ModelUnavailableError after exhausting all providers
        11.7 - Same provider+model sequence for same Role when state is fixed
        11.9 - Per-call timeout of 60s, timeout treated as transient error
        24.7 - Cancel in-flight backoff on user interrupt within 2 seconds
    """

    def __init__(
        self,
        *,
        chain_config: RoleChainConfig,
        registry_checker: RegistryChecker,
        call_adapter: ModelCallAdapter,
        breakers: dict[str, CircuitBreaker] | None = None,
        audit_recorder: AuditRecorder | None = None,
        cancel_event: asyncio.Event | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        event_emitter: EventEmitter | None = None,
        session_budget: SessionBudget | None = None,
        session_id: str = "",
    ) -> None:
        """Initialize the ModelRouter.

        Args:
            chain_config: Role-to-chain mapping from models.yaml.
            registry_checker: Callable that returns True if a provider name
                is present and healthy in the CapabilityRegistry.
            call_adapter: Async callable that performs the actual model call.
                Signature: (provider, model, messages, **kwargs) -> str
            breakers: Optional dict mapping provider names to CircuitBreaker
                instances. Providers not in the dict get a StubCircuitBreaker.
            audit_recorder: Optional async callable that records a DecisionRecord
                in the AuditTrail.
            cancel_event: Optional asyncio.Event for cancelling in-flight backoff.
                When set, any sleeping backoff will be cancelled within 2 seconds.
            call_timeout: Per-call timeout in seconds (default 60s per Req 11.9).
            max_retries: Maximum retries per provider on transient errors (default 3).
            event_emitter: Optional async callable that emits Events for observability.
            session_budget: Optional SessionBudget for pre-call estimation and post-call charging.
            session_id: Session identifier for event emission.
        """
        self._chain_config = chain_config
        self._registry_checker = registry_checker
        self._call_adapter = call_adapter
        self._breakers: dict[str, CircuitBreaker] = breakers or {}
        self._audit_recorder = audit_recorder
        self._cancel_event = cancel_event or asyncio.Event()
        self._call_timeout = call_timeout
        self._max_retries = max_retries
        self._backoff = CancellableBackoff(cancel_event=self._cancel_event)
        self._event_emitter = event_emitter
        self._session_budget = session_budget
        self._session_id = session_id

    @property
    def backoff(self) -> CancellableBackoff:
        """Access the backoff controller (for cancellation from outside)."""
        return self._backoff

    def cancel_backoff(self) -> None:
        """Cancel any in-flight backoff sleep.

        Per requirement 24.7: cancel in-flight backoff on user interrupt.
        """
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Reset the cancel event for a new routing operation."""
        self._cancel_event.clear()
        self._backoff = CancellableBackoff(cancel_event=self._cancel_event)

    def _get_breaker(self, provider: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a provider."""
        if provider not in self._breakers:
            self._breakers[provider] = StubCircuitBreaker()
        return self._breakers[provider]

    def _get_chain(self, role: Role) -> list[ChainEntry]:
        """Get the ordered chain for a role.

        Returns an empty list if the role has no configured chain.
        """
        return self._chain_config.chains.get(role, [])

    async def route(
        self,
        role: Role,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        """Route a Role completion request through the fallback chain with retries.

        Walks the chain in order:
        1. Pre-check session budget (if configured)
        2. Skip providers not in Registry (SkipReason.NOT_IN_REGISTRY)
        3. Skip providers with open circuit breaker (SkipReason.BREAKER_OPEN)
        4. Attempt the call with per-call timeout (60s)
        5. On success: record on breaker, emit model.selected, charge budget, return
        6. On PermanentError: record on breaker, advance to next provider immediately
        7. On transient error: apply exponential backoff, retry up to max_retries
        8. On cancellation: stop retrying, advance to next provider

        Args:
            role: The Role to resolve.
            messages: The messages to complete.
            **kwargs: Additional arguments passed to the call adapter.
                - estimated_tokens: Optional token estimate for budget pre-check.

        Returns:
            The completion string from the first successful provider.

        Raises:
            ModelUnavailableError: If all providers are exhausted.
            BudgetExceededError: If estimated tokens exceed remaining budget.
        """
        # Budget pre-check (Requirement 12.2)
        estimated_tokens = kwargs.pop("estimated_tokens", None)
        if self._session_budget is not None:
            tokens = estimated_tokens if estimated_tokens is not None else DEFAULT_ESTIMATED_TOKENS
            await self._session_budget.check_budget(estimated_tokens=tokens, role=role.value)

        chain = self._get_chain(role)
        attempt_log: list[AttemptRecord] = []
        provider_index = 0  # Track position for event emission
        last_attempted_provider: str | None = None

        for entry in chain:
            provider_index += 1

            # Check registry availability (Requirement 11.2)
            if not self._registry_checker(entry.provider):
                attempt_log.append(
                    AttemptRecord(
                        provider=entry.provider,
                        model=entry.model,
                        status="skipped",
                        reason=SkipReason.NOT_IN_REGISTRY.value,
                    )
                )
                logger.debug(
                    "Skipping provider '%s' for role '%s': not in registry",
                    entry.provider,
                    role.value,
                )
                continue

            # Check circuit breaker (Requirement 11.2)
            breaker = self._get_breaker(entry.provider)
            if breaker.state == CircuitBreakerState.OPEN:
                attempt_log.append(
                    AttemptRecord(
                        provider=entry.provider,
                        model=entry.model,
                        status="skipped",
                        reason=SkipReason.BREAKER_OPEN.value,
                    )
                )
                logger.debug(
                    "Skipping provider '%s' for role '%s': breaker open",
                    entry.provider,
                    role.value,
                )
                continue

            # Emit fallback event if this isn't the first attempted provider
            if last_attempted_provider is not None:
                await self._emit_fallback_event(
                    role, last_attempted_provider, entry.provider, entry.model, provider_index
                )

            # Attempt with retries (Requirement 11.4, 11.5, 11.9)
            result = await self._attempt_with_retries(
                entry, breaker, role, messages, attempt_log, **kwargs
            )
            if result is not None:
                # Emit model.selected event (Requirement 11.8)
                await self._emit_selected_event(role, entry, provider_index)
                # Charge budget (Requirement 12.4)
                if self._session_budget is not None:
                    tokens = estimated_tokens if estimated_tokens is not None else DEFAULT_ESTIMATED_TOKENS
                    self._session_budget.charge(tokens)
                return result

            last_attempted_provider = entry.provider

        # All providers exhausted (Requirement 11.6)
        await self._record_exhaustion(role, attempt_log)
        raise ModelUnavailableError(role=role, attempt_log=attempt_log)

    async def _attempt_with_retries(
        self,
        entry: ChainEntry,
        breaker: CircuitBreaker,
        role: Role,
        messages: list[dict[str, Any]],
        attempt_log: list[AttemptRecord],
        **kwargs: Any,
    ) -> str | None:
        """Attempt a provider call with transient-error retries.

        On transient errors: applies exponential backoff and retries up to
        max_retries times. On permanent errors or cancellation: stops immediately.

        Returns:
            The completion string on success, or None to advance to next provider.
        """
        for attempt in range(self._max_retries + 1):  # Initial attempt + retries
            try:
                # Per-call timeout (Requirement 11.9)
                result = await asyncio.wait_for(
                    self._call_adapter(entry.provider, entry.model, messages, **kwargs),
                    timeout=self._call_timeout,
                )
                # Record success (Requirement 11.3)
                breaker.record_success()
                attempt_log.append(
                    AttemptRecord(
                        provider=entry.provider,
                        model=entry.model,
                        status="success",
                    )
                )
                logger.info(
                    "Model call succeeded: role='%s' provider='%s' model='%s' attempt=%d",
                    role.value,
                    entry.provider,
                    entry.model,
                    attempt + 1,
                )
                return result

            except asyncio.TimeoutError:
                # Timeout is treated as transient error (Requirement 11.9)
                breaker.record_failure()
                attempt_log.append(
                    AttemptRecord(
                        provider=entry.provider,
                        model=entry.model,
                        status="failed",
                        reason="timeout",
                        error=f"Call timed out after {self._call_timeout}s",
                    )
                )
                logger.warning(
                    "Model call timed out: role='%s' provider='%s' attempt=%d/%d",
                    role.value,
                    entry.provider,
                    attempt + 1,
                    self._max_retries + 1,
                )

            except PermanentError as exc:
                # Permanent errors: do NOT retry (Requirement 11.5)
                breaker.record_failure()
                attempt_log.append(
                    AttemptRecord(
                        provider=entry.provider,
                        model=entry.model,
                        status="failed",
                        reason="permanent",
                        error=str(exc),
                    )
                )
                logger.warning(
                    "Permanent error from provider '%s': %s — advancing to next provider",
                    entry.provider,
                    str(exc),
                )
                return None  # Advance to next provider

            except Exception as exc:
                # Transient errors: retry with backoff (Requirement 11.4)
                breaker.record_failure()
                attempt_log.append(
                    AttemptRecord(
                        provider=entry.provider,
                        model=entry.model,
                        status="failed",
                        reason="transient",
                        error=str(exc),
                    )
                )
                logger.warning(
                    "Transient error from provider '%s': %s (attempt %d/%d)",
                    entry.provider,
                    str(exc),
                    attempt + 1,
                    self._max_retries + 1,
                )

            # If we have remaining retries and this was a transient error, backoff
            if attempt < self._max_retries:
                # Apply cancellable exponential backoff (Requirement 11.4, 24.7)
                completed = await self._backoff.wait(attempt)
                if not completed:
                    # Cancelled — stop retrying this provider (Requirement 24.7)
                    logger.info(
                        "Backoff cancelled for provider '%s' — advancing to next provider",
                        entry.provider,
                    )
                    return None

        # All retries exhausted for this provider — advance to next
        logger.info(
            "Max retries (%d) exhausted for provider '%s' — advancing to next provider",
            self._max_retries,
            entry.provider,
        )
        return None

    async def _record_exhaustion(
        self, role: Role, attempt_log: list[AttemptRecord]
    ) -> None:
        """Record the exhaustion attempt log in the AuditTrail.

        Creates a DecisionRecord with kind=MODEL_FALLBACK documenting the
        full attempt sequence for explainability.

        Args:
            role: The Role that could not be served.
            attempt_log: The full list of attempt records.
        """
        if self._audit_recorder is None:
            logger.debug("No audit recorder configured; skipping exhaustion record")
            return

        record = DecisionRecord(
            kind=DecisionKind.MODEL_FALLBACK,
            subject=f"role:{role.value}",
            inputs={
                "role": role.value,
                "chain_length": len(self._get_chain(role)),
                "attempts": [
                    {
                        "provider": a.provider,
                        "model": a.model,
                        "status": a.status,
                        "reason": a.reason,
                        "error": a.error,
                    }
                    for a in attempt_log
                ],
            },
            decision="model_unavailable",
            rationale=(
                f"All {len(attempt_log)} providers in the chain for role "
                f"'{role.value}' were either skipped or failed. "
                f"Providers attempted: "
                + ", ".join(
                    f"{a.provider}({a.status})" for a in attempt_log
                )
            ),
            alternatives=[a.provider for a in attempt_log if a.status == "skipped"],
        )

        try:
            await self._audit_recorder(record)
        except Exception:
            logger.exception(
                "Failed to record model exhaustion in audit trail for role '%s'",
                role.value,
            )

    async def _emit_selected_event(
        self, role: Role, entry: ChainEntry, attempt: int
    ) -> None:
        """Emit a model.selected event on successful provider selection.

        Requirement 11.8: emit model.selected carrying Role, provider, model,
        attempt, and reason.
        """
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.MODEL_SELECTED,
            session_id=self._session_id,
            source="model_router",
            payload={
                "role": role.value,
                "provider": entry.provider,
                "model": entry.model,
                "attempt": attempt,
                "reason": "success",
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )
        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception("Failed to emit model.selected event")

    async def _emit_fallback_event(
        self,
        role: Role,
        from_provider: str,
        to_provider: str,
        to_model: str,
        attempt: int,
    ) -> None:
        """Emit a model.fallback event when falling back between providers.

        Requirement 11.8: emit model.fallback carrying Role, provider, model,
        attempt, and reason.
        """
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.MODEL_FALLBACK,
            session_id=self._session_id,
            source="model_router",
            payload={
                "role": role.value,
                "from_provider": from_provider,
                "to_provider": to_provider,
                "model": to_model,
                "attempt": attempt,
                "reason": "previous_provider_failed",
            },
            correlation_id=self._session_id,
            event_id=str(uuid.uuid4()),
        )
        try:
            await self._event_emitter(event)
        except Exception:
            logger.exception("Failed to emit model.fallback event")
