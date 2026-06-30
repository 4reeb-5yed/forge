"""Unit tests for SessionBudget token/cost governance.

Tests cover:
- Initialization with configured token limit and consumed=0 (Req 12.1)
- Pre-call estimation check raises BudgetExceededError (Req 12.2)
- Event emission on budget exceeded (Req 12.3)
- Post-call charge adds actual usage (Req 12.4)
- Expose limit, consumed, remaining via properties (Req 12.5)
"""

from __future__ import annotations

import pytest

from app.runtime.budget import BudgetExceededError, SessionBudget
from app.runtime.events.models import EventType


class TestSessionBudgetInitialization:
    """Requirement 12.1: Initialize per-session budget to configured token limit."""

    def test_initial_consumed_is_zero(self):
        budget = SessionBudget(token_limit=1_000_000, session_id="s1")
        assert budget.consumed == 0

    def test_initial_limit_matches_config(self):
        budget = SessionBudget(token_limit=500_000, session_id="s1")
        assert budget.limit == 500_000

    def test_initial_remaining_equals_limit(self):
        budget = SessionBudget(token_limit=1_000_000, session_id="s1")
        assert budget.remaining == 1_000_000

    def test_session_id_stored(self):
        budget = SessionBudget(token_limit=100, session_id="test-session")
        assert budget.session_id == "test-session"

    def test_negative_limit_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            SessionBudget(token_limit=-1, session_id="s1")

    def test_zero_limit_allowed(self):
        budget = SessionBudget(token_limit=0, session_id="s1")
        assert budget.limit == 0
        assert budget.remaining == 0


class TestSessionBudgetPreCallCheck:
    """Requirement 12.2: Pre-call estimation check raises BudgetExceededError."""

    @pytest.mark.asyncio
    async def test_check_passes_when_under_budget(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        # Should not raise
        await budget.check_budget(estimated_tokens=500, role="coder")

    @pytest.mark.asyncio
    async def test_check_passes_at_exact_limit(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        # Exactly at limit should pass
        await budget.check_budget(estimated_tokens=1000, role="coder")

    @pytest.mark.asyncio
    async def test_check_raises_when_over_budget(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        with pytest.raises(BudgetExceededError) as exc_info:
            await budget.check_budget(estimated_tokens=1001, role="coder")
        assert exc_info.value.role == "coder"
        assert exc_info.value.estimated_tokens == 1001
        assert exc_info.value.remaining == 1000

    @pytest.mark.asyncio
    async def test_check_raises_after_partial_consumption(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        budget.charge(800)
        with pytest.raises(BudgetExceededError) as exc_info:
            await budget.check_budget(estimated_tokens=201, role="architect")
        assert exc_info.value.remaining == 200

    @pytest.mark.asyncio
    async def test_check_does_not_modify_consumed_on_failure(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        budget.charge(900)
        try:
            await budget.check_budget(estimated_tokens=200, role="coder")
        except BudgetExceededError:
            pass
        # consumed should remain unchanged
        assert budget.consumed == 900

    @pytest.mark.asyncio
    async def test_check_raises_on_zero_remaining(self):
        budget = SessionBudget(token_limit=100, session_id="s1")
        budget.charge(100)
        with pytest.raises(BudgetExceededError):
            await budget.check_budget(estimated_tokens=1, role="coder")


class TestSessionBudgetEventEmission:
    """Requirement 12.3: Emit budget.exceeded event when budget is exceeded."""

    @pytest.mark.asyncio
    async def test_emits_budget_exceeded_event(self):
        emitted_events = []

        async def mock_emitter(event):
            emitted_events.append(event)

        budget = SessionBudget(
            token_limit=100, session_id="s1", event_emitter=mock_emitter
        )
        with pytest.raises(BudgetExceededError):
            await budget.check_budget(estimated_tokens=200, role="reviewer")

        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event.type == EventType.BUDGET_EXCEEDED
        assert event.session_id == "s1"
        assert event.source == "session_budget"
        assert event.payload["role"] == "reviewer"
        assert event.payload["estimated_tokens"] == 200
        assert event.payload["remaining"] == 100
        assert event.payload["limit"] == 100
        assert event.payload["consumed"] == 0

    @pytest.mark.asyncio
    async def test_no_event_emitted_when_under_budget(self):
        emitted_events = []

        async def mock_emitter(event):
            emitted_events.append(event)

        budget = SessionBudget(
            token_limit=1000, session_id="s1", event_emitter=mock_emitter
        )
        await budget.check_budget(estimated_tokens=500, role="coder")

        assert len(emitted_events) == 0

    @pytest.mark.asyncio
    async def test_no_error_without_event_emitter(self):
        """Budget works without an event emitter (emitter is optional)."""
        budget = SessionBudget(token_limit=100, session_id="s1")
        with pytest.raises(BudgetExceededError):
            await budget.check_budget(estimated_tokens=200, role="coder")


class TestSessionBudgetPostCallCharge:
    """Requirement 12.4: Post-call charge adds actual token usage to consumed."""

    def test_charge_adds_to_consumed(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        budget.charge(150)
        assert budget.consumed == 150
        assert budget.remaining == 850

    def test_multiple_charges_accumulate(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        budget.charge(100)
        budget.charge(200)
        budget.charge(50)
        assert budget.consumed == 350
        assert budget.remaining == 650

    def test_charge_zero_is_valid(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        budget.charge(0)
        assert budget.consumed == 0

    def test_negative_charge_rejected(self):
        budget = SessionBudget(token_limit=1000, session_id="s1")
        with pytest.raises(ValueError, match="non-negative"):
            budget.charge(-10)

    def test_charge_can_exceed_limit(self):
        """Actual usage may exceed estimates; charge records truth."""
        budget = SessionBudget(token_limit=100, session_id="s1")
        budget.charge(150)
        assert budget.consumed == 150
        assert budget.remaining == -50


class TestSessionBudgetInspectorExposure:
    """Requirement 12.5: Expose limit, consumed, remaining via RuntimeInspector."""

    def test_summary_returns_all_fields(self):
        budget = SessionBudget(token_limit=1_000_000, session_id="s1")
        budget.charge(250_000)
        summary = budget.summary()
        assert summary == {
            "limit": 1_000_000,
            "consumed": 250_000,
            "remaining": 750_000,
        }

    def test_summary_at_initialization(self):
        budget = SessionBudget(token_limit=500, session_id="s1")
        summary = budget.summary()
        assert summary["limit"] == 500
        assert summary["consumed"] == 0
        assert summary["remaining"] == 500

    def test_properties_exposed(self):
        budget = SessionBudget(token_limit=2000, session_id="s1")
        budget.charge(500)
        assert budget.limit == 2000
        assert budget.consumed == 500
        assert budget.remaining == 1500


class TestBudgetExceededError:
    """Test the BudgetExceededError exception itself."""

    def test_error_attributes(self):
        err = BudgetExceededError(role="coder", estimated_tokens=500, remaining=100)
        assert err.role == "coder"
        assert err.estimated_tokens == 500
        assert err.remaining == 100

    def test_error_message(self):
        err = BudgetExceededError(role="architect", estimated_tokens=1000, remaining=200)
        assert "architect" in str(err)
        assert "1000" in str(err)
        assert "200" in str(err)

    def test_is_exception(self):
        err = BudgetExceededError(role="coder", estimated_tokens=10, remaining=5)
        assert isinstance(err, Exception)


class TestBudgetCheckChargeIntegration:
    """Integration tests for the check_budget -> charge workflow."""

    @pytest.mark.asyncio
    async def test_check_does_not_modify_consumed_on_success(self):
        """A passing check_budget is purely read-only (Req 12.2 — leave consumed unchanged)."""
        budget = SessionBudget(token_limit=1000, session_id="s1")
        await budget.check_budget(estimated_tokens=500, role="coder")
        assert budget.consumed == 0
        assert budget.remaining == 1000

    @pytest.mark.asyncio
    async def test_full_lifecycle_check_then_charge(self):
        """Full cycle: check passes, charge updates consumed, next check uses new remaining."""
        budget = SessionBudget(token_limit=1000, session_id="s1")

        # First call: estimate 400, charge 350
        await budget.check_budget(estimated_tokens=400, role="coder")
        budget.charge(350)
        assert budget.consumed == 350
        assert budget.remaining == 650

        # Second call: estimate 600, should pass
        await budget.check_budget(estimated_tokens=650, role="reviewer")
        budget.charge(600)
        assert budget.consumed == 950
        assert budget.remaining == 50

        # Third call: estimate 51, should exceed remaining of 50
        with pytest.raises(BudgetExceededError) as exc_info:
            await budget.check_budget(estimated_tokens=51, role="coder")
        assert exc_info.value.remaining == 50

    @pytest.mark.asyncio
    async def test_multiple_checks_without_charge_do_not_consume(self):
        """Multiple sequential check_budget calls don't consume budget."""
        budget = SessionBudget(token_limit=1000, session_id="s1")
        await budget.check_budget(estimated_tokens=500, role="coder")
        await budget.check_budget(estimated_tokens=500, role="reviewer")
        await budget.check_budget(estimated_tokens=500, role="architect")
        # Nothing charged yet
        assert budget.consumed == 0
        assert budget.remaining == 1000
