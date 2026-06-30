"""Property-based tests for SessionBudget safety.

**Property 8: Budget safety** — no model call is issued when
session.budget.remaining < estimated_tokens.

**Validates: Requirements 12.2, 12.4**

Uses Hypothesis to generate arbitrary budgets (limit, consumed amounts,
estimated amounts) and verifies:
- For any combination where estimated > remaining, check_budget() raises BudgetExceededError
- For any combination where estimated <= remaining, check_budget() passes
- After charging, remaining decreases correctly
- Sequences of charges followed by checks maintain the invariant throughout a session lifecycle
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.runtime.budget import BudgetExceededError, SessionBudget


# --- Strategies ---

# Token limits: realistic range from 0 to 10M tokens
token_limits = st.integers(min_value=0, max_value=10_000_000)

# Consumed amounts: 0 up to some reasonable value
consumed_amounts = st.integers(min_value=0, max_value=10_000_000)

# Estimated amounts: 0 up to some reasonable value
estimated_amounts = st.integers(min_value=0, max_value=10_000_000)

# Roles for the budget check
roles = st.sampled_from(["coder", "architect", "reviewer", "planner", "doc_writer"])

# A charge operation: (amount_to_charge, then_estimate, role)
charge_then_check = st.tuples(
    st.integers(min_value=0, max_value=1_000_000),  # charge amount
    st.integers(min_value=0, max_value=1_000_000),  # estimate for check
    roles,
)

# A sequence of charge operations to simulate a session lifecycle
charge_sequence = st.lists(
    st.integers(min_value=0, max_value=100_000),
    min_size=0,
    max_size=20,
)


class TestBudgetSafetyProperty:
    """Property 8: Budget safety — no model call is issued when
    session.budget.remaining < estimated_tokens.

    **Validates: Requirements 12.2, 12.4**
    """

    @given(
        token_limit=token_limits,
        consumed=consumed_amounts,
        estimated=estimated_amounts,
        role=roles,
    )
    @settings(max_examples=500)
    def test_check_budget_raises_when_estimated_exceeds_remaining(
        self, token_limit: int, consumed: int, estimated: int, role: str
    ):
        """For any combination where estimated > remaining, check_budget() raises
        BudgetExceededError. For any combination where estimated <= remaining,
        check_budget() passes without error.

        This is the core budget safety invariant: the system never allows a model
        call when the budget cannot accommodate the estimated token usage.
        """
        # Constrain consumed to not exceed the limit (we can't charge more than
        # the limit naturally in one step, but charge() allows it; we test
        # the pre-condition where consumed <= limit for the safety property)
        consumed = min(consumed, token_limit)

        budget = SessionBudget(token_limit=token_limit, session_id="prop-test")
        if consumed > 0:
            budget.charge(consumed)

        remaining = token_limit - consumed
        assert budget.remaining == remaining

        if estimated > remaining:
            # Budget safety: MUST raise, preventing the model call
            with pytest.raises(BudgetExceededError) as exc_info:
                asyncio.run(
                    budget.check_budget(estimated_tokens=estimated, role=role)
                )
            # Verify error carries correct information
            assert exc_info.value.estimated_tokens == estimated
            assert exc_info.value.remaining == remaining
            assert exc_info.value.role == role
            # Consumed must NOT change on rejection (no model call issued)
            assert budget.consumed == consumed
        else:
            # Budget sufficient: check should pass without raising
            asyncio.run(
                budget.check_budget(estimated_tokens=estimated, role=role)
            )
            # Consumed should remain unchanged (check doesn't charge)
            assert budget.consumed == consumed

    @given(
        token_limit=st.integers(min_value=1, max_value=10_000_000),
        charges=charge_sequence,
        final_estimate=st.integers(min_value=0, max_value=1_000_000),
        role=roles,
    )
    @settings(max_examples=300)
    def test_charge_then_check_lifecycle_invariant(
        self,
        token_limit: int,
        charges: list[int],
        final_estimate: int,
        role: str,
    ):
        """Test sequences of charges followed by checks to ensure the budget
        safety invariant holds throughout a session lifecycle.

        After each charge, remaining must decrease by exactly the charged amount.
        After the sequence of charges, a budget check must respect the final
        remaining value: raise if estimated > remaining, pass otherwise.
        """
        budget = SessionBudget(token_limit=token_limit, session_id="lifecycle-test")

        total_charged = 0
        for charge_amount in charges:
            prev_remaining = budget.remaining
            budget.charge(charge_amount)
            total_charged += charge_amount

            # Invariant: remaining decreases by exactly charge_amount
            assert budget.remaining == prev_remaining - charge_amount
            # Invariant: consumed tracks total
            assert budget.consumed == total_charged
            # Invariant: remaining = limit - consumed
            assert budget.remaining == token_limit - total_charged

        # Final check: the safety property must hold
        remaining = budget.remaining
        if final_estimate > remaining:
            with pytest.raises(BudgetExceededError):
                asyncio.run(
                    budget.check_budget(estimated_tokens=final_estimate, role=role)
                )
            # Budget unchanged after rejection
            assert budget.consumed == total_charged
        else:
            asyncio.run(
                budget.check_budget(estimated_tokens=final_estimate, role=role)
            )
            # Budget unchanged after passing check (check doesn't charge)
            assert budget.consumed == total_charged

    @given(
        token_limit=st.integers(min_value=1, max_value=5_000_000),
        charge_amount=st.integers(min_value=0, max_value=5_000_000),
    )
    @settings(max_examples=300)
    def test_remaining_decreases_correctly_after_charge(
        self, token_limit: int, charge_amount: int
    ):
        """After charging, verify remaining decreases by exactly the charged
        amount. This ensures post-call accounting is correct (Req 12.4).
        """
        budget = SessionBudget(token_limit=token_limit, session_id="charge-test")

        initial_remaining = budget.remaining
        assert initial_remaining == token_limit

        budget.charge(charge_amount)

        assert budget.consumed == charge_amount
        assert budget.remaining == token_limit - charge_amount
        assert budget.remaining == initial_remaining - charge_amount

    @given(
        token_limit=st.integers(min_value=0, max_value=10_000_000),
        estimated=estimated_amounts,
        role=roles,
    )
    @settings(max_examples=300)
    def test_fresh_budget_safety(self, token_limit: int, estimated: int, role: str):
        """On a fresh budget (consumed=0), the safety invariant holds:
        estimated > limit means rejection, estimated <= limit means acceptance.
        """
        budget = SessionBudget(token_limit=token_limit, session_id="fresh-test")
        assert budget.remaining == token_limit
        assert budget.consumed == 0

        if estimated > token_limit:
            with pytest.raises(BudgetExceededError) as exc_info:
                asyncio.run(
                    budget.check_budget(estimated_tokens=estimated, role=role)
                )
            assert exc_info.value.remaining == token_limit
            assert exc_info.value.estimated_tokens == estimated
        else:
            asyncio.run(
                budget.check_budget(estimated_tokens=estimated, role=role)
            )
            # No side effects from a passing check
            assert budget.consumed == 0
            assert budget.remaining == token_limit

    @given(
        token_limit=st.integers(min_value=1, max_value=1_000_000),
        ops=st.lists(
            st.tuples(
                st.sampled_from(["charge", "check"]),
                st.integers(min_value=0, max_value=200_000),
                roles,
            ),
            min_size=1,
            max_size=30,
        ),
    )
    @settings(max_examples=200)
    def test_interleaved_charges_and_checks_maintain_invariant(
        self,
        token_limit: int,
        ops: list[tuple[str, int, str]],
    ):
        """Interleave charge and check operations. At every check point, the
        budget safety invariant must hold: estimated > remaining means rejection.
        """
        budget = SessionBudget(token_limit=token_limit, session_id="interleave-test")

        for op_type, amount, role in ops:
            if op_type == "charge":
                budget.charge(amount)
            else:
                # op_type == "check"
                remaining = budget.remaining
                if amount > remaining:
                    with pytest.raises(BudgetExceededError):
                        asyncio.run(
                            budget.check_budget(estimated_tokens=amount, role=role)
                        )
                else:
                    asyncio.run(
                        budget.check_budget(estimated_tokens=amount, role=role)
                    )
