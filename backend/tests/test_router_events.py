"""Unit tests for ModelRouter event emission and budget integration.

Tests task 5.3: model.selected and model.fallback event emission,
SessionBudget pre-call estimation check and post-call charge, and
budget.exceeded event propagation.

Requirements: 11.8, 12.2, 12.3, 12.4
"""

from __future__ import annotations

import pytest

from app.runtime.budget import BudgetExceededError, SessionBudget
from app.runtime.events.models import Event, EventType
from app.runtime.models import Role
from app.runtime.router import (
    ChainEntry,
    CircuitBreakerState,
    ModelRouter,
    ModelUnavailableError,
    RoleChainConfig,
    StubCircuitBreaker,
)


# --- Test helpers ---


class FakeBreaker:
    """Controllable circuit breaker for testing."""

    def __init__(self, state: CircuitBreakerState = CircuitBreakerState.CLOSED):
        self._state = state
        self.successes = 0
        self.failures = 0

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


def make_chain_config(
    role: Role, entries: list[tuple[str, str]]
) -> RoleChainConfig:
    """Create a RoleChainConfig from a list of (provider, model) tuples."""
    return RoleChainConfig(
        chains={role: [ChainEntry(provider=p, model=m) for p, m in entries]}
    )


def make_call_adapter(responses: dict[str, str | Exception]):
    """Create a call adapter that returns or raises based on provider name.

    Args:
        responses: Mapping of provider name to either a string result or
            an Exception to raise.
    """

    async def adapter(provider: str, model: str, messages: list, **kwargs):
        result = responses.get(provider)
        if isinstance(result, Exception):
            raise result
        return result or f"response_from_{provider}"

    return adapter


class EventCollector:
    """Collects emitted events for assertions."""

    def __init__(self):
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


# --- Tests for model.selected event emission ---


class TestModelSelectedEvent:
    """Tests for Requirement 11.8: emit model.selected on successful selection."""

    async def test_emits_model_selected_on_success(self):
        """model.selected event is emitted when a provider call succeeds."""
        collector = EventCollector()
        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "hello"}),
            event_emitter=collector,
            session_id="sess-1",
        )

        result = await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        assert result == "hello"
        selected_events = collector.of_type(EventType.MODEL_SELECTED)
        assert len(selected_events) == 1
        payload = selected_events[0].payload
        assert payload["role"] == "coder"
        assert payload["provider"] == "openrouter"
        assert payload["model"] == "gpt-4"
        assert payload["attempt"] == 1
        assert payload["reason"] == "success"

    async def test_model_selected_carries_correct_attempt_number_after_fallback(self):
        """model.selected attempt number reflects chain position after failures."""
        collector = EventCollector()
        config = make_chain_config(
            Role.CODER,
            [("provider_a", "model-a"), ("provider_b", "model-b")],
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({
                "provider_a": RuntimeError("down"),
                "provider_b": "success",
            }),
            event_emitter=collector,
            session_id="sess-2",
        )

        result = await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        assert result == "success"
        selected_events = collector.of_type(EventType.MODEL_SELECTED)
        assert len(selected_events) == 1
        assert selected_events[0].payload["provider"] == "provider_b"
        assert selected_events[0].payload["attempt"] == 2

    async def test_no_model_selected_when_all_fail(self):
        """No model.selected event if all providers fail."""
        collector = EventCollector()
        config = make_chain_config(Role.CODER, [("provider_a", "model-a")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"provider_a": RuntimeError("down")}),
            event_emitter=collector,
            session_id="sess-3",
        )

        with pytest.raises(ModelUnavailableError):
            await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        assert len(collector.of_type(EventType.MODEL_SELECTED)) == 0

    async def test_model_selected_event_has_session_id(self):
        """model.selected event carries the correct session_id."""
        collector = EventCollector()
        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "hello"}),
            event_emitter=collector,
            session_id="my-session-42",
        )

        await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        selected_events = collector.of_type(EventType.MODEL_SELECTED)
        assert selected_events[0].session_id == "my-session-42"


# --- Tests for model.fallback event emission ---


class TestModelFallbackEvent:
    """Tests for Requirement 11.8: emit model.fallback when falling back."""

    async def test_emits_model_fallback_on_provider_failure(self):
        """model.fallback event is emitted when falling back from one provider to next."""
        collector = EventCollector()
        config = make_chain_config(
            Role.CODER,
            [("provider_a", "model-a"), ("provider_b", "model-b")],
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({
                "provider_a": RuntimeError("timeout"),
                "provider_b": "success",
            }),
            event_emitter=collector,
            session_id="sess-fb-1",
        )

        await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        fallback_events = collector.of_type(EventType.MODEL_FALLBACK)
        assert len(fallback_events) == 1
        payload = fallback_events[0].payload
        assert payload["role"] == "coder"
        assert payload["from_provider"] == "provider_a"
        assert payload["to_provider"] == "provider_b"
        assert payload["model"] == "model-b"
        assert payload["attempt"] == 2
        assert payload["reason"] == "previous_provider_failed"

    async def test_emits_multiple_fallback_events_for_chain(self):
        """Multiple fallback events are emitted for a 3-provider chain."""
        collector = EventCollector()
        config = make_chain_config(
            Role.CODER,
            [("a", "ma"), ("b", "mb"), ("c", "mc")],
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({
                "a": RuntimeError("fail"),
                "b": RuntimeError("fail"),
                "c": "done",
            }),
            event_emitter=collector,
            session_id="sess-fb-2",
        )

        await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        fallback_events = collector.of_type(EventType.MODEL_FALLBACK)
        assert len(fallback_events) == 2
        assert fallback_events[0].payload["from_provider"] == "a"
        assert fallback_events[0].payload["to_provider"] == "b"
        assert fallback_events[1].payload["from_provider"] == "b"
        assert fallback_events[1].payload["to_provider"] == "c"

    async def test_no_fallback_event_on_first_provider_success(self):
        """No model.fallback event when the first provider succeeds."""
        collector = EventCollector()
        config = make_chain_config(
            Role.CODER,
            [("provider_a", "model-a"), ("provider_b", "model-b")],
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({
                "provider_a": "success",
                "provider_b": "also-success",
            }),
            event_emitter=collector,
            session_id="sess-fb-3",
        )

        await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        assert len(collector.of_type(EventType.MODEL_FALLBACK)) == 0

    async def test_no_fallback_event_for_skipped_providers(self):
        """Skipped providers (not in registry) do not trigger fallback events."""
        collector = EventCollector()
        config = make_chain_config(
            Role.CODER,
            [("missing_a", "model-a"), ("present_b", "model-b")],
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: p == "present_b",
            call_adapter=make_call_adapter({"present_b": "success"}),
            event_emitter=collector,
            session_id="sess-fb-4",
        )

        await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        # No fallback since skipped providers aren't attempted
        assert len(collector.of_type(EventType.MODEL_FALLBACK)) == 0


# --- Tests for no event_emitter (graceful no-op) ---


class TestNoEventEmitter:
    """Tests that routing works correctly without an event emitter."""

    async def test_route_succeeds_without_event_emitter(self):
        """Router works without event_emitter configured."""
        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "hello"}),
            # No event_emitter
        )

        result = await router.route(Role.CODER, [{"role": "user", "content": "hi"}])
        assert result == "hello"

    async def test_fallback_works_without_event_emitter(self):
        """Fallback logic works without event_emitter."""
        config = make_chain_config(
            Role.CODER,
            [("a", "ma"), ("b", "mb")],
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({
                "a": RuntimeError("down"),
                "b": "ok",
            }),
        )

        result = await router.route(Role.CODER, [{"role": "user", "content": "hi"}])
        assert result == "ok"


# --- Tests for budget integration ---


class TestBudgetIntegration:
    """Tests for Requirements 12.2, 12.3, 12.4: budget pre-check and post-charge."""

    async def test_budget_check_before_call(self):
        """BudgetExceededError raised when estimated tokens exceed remaining budget."""
        budget = SessionBudget(token_limit=100, session_id="sess-budget-1")
        # Consume most of the budget
        budget.charge(90)

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "should_not_reach"}),
            session_budget=budget,
            session_id="sess-budget-1",
        )

        with pytest.raises(BudgetExceededError) as exc_info:
            await router.route(
                Role.CODER,
                [{"role": "user", "content": "hi"}],
                estimated_tokens=50,
            )

        assert exc_info.value.role == "coder"
        assert exc_info.value.estimated_tokens == 50
        assert exc_info.value.remaining == 10

    async def test_budget_not_charged_when_check_fails(self):
        """Budget consumed amount is unchanged when pre-check fails."""
        budget = SessionBudget(token_limit=100, session_id="sess-budget-2")
        budget.charge(95)
        initial_consumed = budget.consumed

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-budget-2",
        )

        with pytest.raises(BudgetExceededError):
            await router.route(
                Role.CODER,
                [{"role": "user", "content": "hi"}],
                estimated_tokens=20,
            )

        assert budget.consumed == initial_consumed

    async def test_budget_charged_after_successful_call(self):
        """Budget consumed increases by estimated_tokens after successful call."""
        budget = SessionBudget(token_limit=1000, session_id="sess-budget-3")
        assert budget.consumed == 0

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-budget-3",
        )

        await router.route(
            Role.CODER,
            [{"role": "user", "content": "hi"}],
            estimated_tokens=200,
        )

        assert budget.consumed == 200
        assert budget.remaining == 800

    async def test_budget_not_charged_on_failed_call(self):
        """Budget is not charged when all providers fail."""
        budget = SessionBudget(token_limit=1000, session_id="sess-budget-4")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": RuntimeError("down")}),
            session_budget=budget,
            session_id="sess-budget-4",
        )

        with pytest.raises(ModelUnavailableError):
            await router.route(
                Role.CODER,
                [{"role": "user", "content": "hi"}],
                estimated_tokens=200,
            )

        assert budget.consumed == 0

    async def test_budget_check_passes_when_within_limit(self):
        """Route succeeds when estimated tokens are within budget."""
        budget = SessionBudget(token_limit=1000, session_id="sess-budget-5")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-budget-5",
        )

        result = await router.route(
            Role.CODER,
            [{"role": "user", "content": "hi"}],
            estimated_tokens=500,
        )

        assert result == "result"

    async def test_no_budget_check_when_budget_not_configured(self):
        """Route works without a SessionBudget (no budget enforcement)."""
        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            # No session_budget
        )

        result = await router.route(
            Role.CODER,
            [{"role": "user", "content": "hi"}],
            estimated_tokens=999999,
        )

        assert result == "result"

    async def test_budget_exceeded_emits_event_via_budget(self):
        """BudgetExceededError propagates; budget emits event internally."""
        emitted_events: list[Event] = []

        async def event_emitter(event: Event) -> None:
            emitted_events.append(event)

        budget = SessionBudget(
            token_limit=50,
            session_id="sess-budget-6",
            event_emitter=event_emitter,
        )

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            event_emitter=event_emitter,
            session_id="sess-budget-6",
        )

        with pytest.raises(BudgetExceededError):
            await router.route(
                Role.CODER,
                [{"role": "user", "content": "hi"}],
                estimated_tokens=100,
            )

        # The budget itself emits a budget.exceeded event
        budget_events = [e for e in emitted_events if e.type == EventType.BUDGET_EXCEEDED]
        assert len(budget_events) == 1
        assert budget_events[0].payload["role"] == "coder"
        assert budget_events[0].payload["estimated_tokens"] == 100
        assert budget_events[0].payload["remaining"] == 50

    async def test_default_estimated_tokens_used_when_not_specified(self):
        """Default estimated_tokens (1000) is used for budget check if not specified."""
        budget = SessionBudget(token_limit=500, session_id="sess-budget-7")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-budget-7",
        )

        # Default is 1000, budget is 500 => should fail
        with pytest.raises(BudgetExceededError) as exc_info:
            await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        assert exc_info.value.estimated_tokens == 1000


# --- Tests for combined event emission + budget ---


class TestEventAndBudgetCombined:
    """Tests for combined event emission and budget in a single routing call."""

    async def test_selected_event_and_budget_charge_together(self):
        """Both model.selected event and budget charge happen on success."""
        collector = EventCollector()
        budget = SessionBudget(token_limit=1000, session_id="sess-combo-1")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            event_emitter=collector,
            session_budget=budget,
            session_id="sess-combo-1",
        )

        result = await router.route(
            Role.CODER,
            [{"role": "user", "content": "hi"}],
            estimated_tokens=300,
        )

        assert result == "result"
        assert budget.consumed == 300
        assert len(collector.of_type(EventType.MODEL_SELECTED)) == 1

    async def test_fallback_event_and_budget_charge_on_second_provider(self):
        """Fallback event emitted and budget charged on second-provider success."""
        collector = EventCollector()
        budget = SessionBudget(token_limit=1000, session_id="sess-combo-2")

        config = make_chain_config(
            Role.CODER,
            [("a", "ma"), ("b", "mb")],
        )
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({
                "a": RuntimeError("fail"),
                "b": "result",
            }),
            event_emitter=collector,
            session_budget=budget,
            session_id="sess-combo-2",
        )

        result = await router.route(
            Role.CODER,
            [{"role": "user", "content": "hi"}],
            estimated_tokens=150,
        )

        assert result == "result"
        assert budget.consumed == 150
        assert len(collector.of_type(EventType.MODEL_FALLBACK)) == 1
        assert len(collector.of_type(EventType.MODEL_SELECTED)) == 1


# --- Additional tests for task 5.5 coverage gaps ---


class TestBudgetDefaultChargeOnSuccess:
    """Tests for budget charging behavior when estimated_tokens is not specified."""

    async def test_default_tokens_charged_on_success_when_not_specified(self):
        """Req 12.4: When estimated_tokens not specified, default (1000) is charged on success."""
        budget = SessionBudget(token_limit=5000, session_id="sess-default-charge")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-default-charge",
        )

        # Route without specifying estimated_tokens
        result = await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        assert result == "result"
        # Default estimated_tokens (1000) should be charged
        assert budget.consumed == 1000
        assert budget.remaining == 4000

    async def test_explicit_tokens_charged_on_success(self):
        """Req 12.4: When estimated_tokens is explicitly provided, that amount is charged."""
        budget = SessionBudget(token_limit=5000, session_id="sess-explicit-charge")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-explicit-charge",
        )

        await router.route(
            Role.CODER,
            [{"role": "user", "content": "hi"}],
            estimated_tokens=250,
        )

        assert budget.consumed == 250
        assert budget.remaining == 4750

    async def test_budget_pre_check_uses_default_when_budget_tight(self):
        """Req 12.2: Pre-check uses default 1000 tokens when not specified and budget is tight."""
        budget = SessionBudget(token_limit=999, session_id="sess-tight")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-tight",
        )

        # Default is 1000, budget is 999 => should fail pre-check
        with pytest.raises(BudgetExceededError) as exc_info:
            await router.route(Role.CODER, [{"role": "user", "content": "hi"}])

        assert exc_info.value.estimated_tokens == 1000
        assert exc_info.value.remaining == 999

    async def test_budget_check_exactly_at_boundary(self):
        """Req 12.2: Budget check passes when estimated equals remaining exactly."""
        budget = SessionBudget(token_limit=500, session_id="sess-boundary")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-boundary",
        )

        # estimated_tokens == remaining (500 == 500) — should pass
        result = await router.route(
            Role.CODER,
            [{"role": "user", "content": "hi"}],
            estimated_tokens=500,
        )

        assert result == "result"
        assert budget.consumed == 500
        assert budget.remaining == 0

    async def test_budget_check_one_over_boundary(self):
        """Req 12.2: Budget check fails when estimated is one over remaining."""
        budget = SessionBudget(token_limit=500, session_id="sess-over")

        config = make_chain_config(Role.CODER, [("openrouter", "gpt-4")])
        router = ModelRouter(
            chain_config=config,
            registry_checker=lambda p: True,
            call_adapter=make_call_adapter({"openrouter": "result"}),
            session_budget=budget,
            session_id="sess-over",
        )

        with pytest.raises(BudgetExceededError) as exc_info:
            await router.route(
                Role.CODER,
                [{"role": "user", "content": "hi"}],
                estimated_tokens=501,
            )

        assert exc_info.value.estimated_tokens == 501
        assert exc_info.value.remaining == 500
