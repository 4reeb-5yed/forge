"""Session budget governance for the Forge Runtime.

Implements per-session token/cost limits enforced before model calls are issued.
The SessionBudget tracks a configured token limit, consumed tokens, and remaining
budget. It raises BudgetExceededError when a pre-call estimation exceeds the
remaining budget, and emits a `budget.exceeded` event via an optional event emitter.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType


class BudgetExceededError(Exception):
    """Raised when estimated token usage exceeds the remaining session budget.

    Per requirement 12.2: if estimated tokens exceed remaining budget, raise this
    error identifying the requested Role and the remaining budget.
    """

    def __init__(self, role: str, estimated_tokens: int, remaining: int) -> None:
        self.role = role
        self.estimated_tokens = estimated_tokens
        self.remaining = remaining
        super().__init__(
            f"Budget exceeded for role '{role}': "
            f"estimated {estimated_tokens} tokens but only {remaining} remaining"
        )


# Type alias for the event emission callback
EventEmitter = Callable[[Event], Awaitable[Any]]


class SessionBudget:
    """Per-session token budget with pre-call estimation and post-call charging.

    Initialized with a token limit (from rate_limits.yaml session.max_tokens).
    Consumed starts at 0. The Model Router calls check_budget() before issuing
    a model call and charge() after the call completes.

    The RuntimeInspector exposes limit, consumed, and remaining via properties.

    Requirements:
        12.1 - Initialize budget to configured limit with consumed=0
        12.2 - Pre-call check raises BudgetExceededError if estimated > remaining
        12.3 - Emit budget.exceeded event when check fails
        12.4 - Post-call charge adds actual usage to consumed
        12.5 - Expose limit, consumed, remaining via Inspector
    """

    def __init__(
        self,
        token_limit: int,
        session_id: str = "",
        event_emitter: EventEmitter | None = None,
    ) -> None:
        """Initialize a session budget.

        Args:
            token_limit: Maximum tokens allowed for this session.
            session_id: The session this budget belongs to.
            event_emitter: Optional async callback to emit events on the bus.
        """
        if token_limit < 0:
            raise ValueError("token_limit must be non-negative")
        self._limit = token_limit
        self._consumed = 0
        self._session_id = session_id
        self._event_emitter = event_emitter

    @property
    def limit(self) -> int:
        """The configured per-session token limit."""
        return self._limit

    @property
    def consumed(self) -> int:
        """Total tokens consumed so far in this session."""
        return self._consumed

    @property
    def remaining(self) -> int:
        """Tokens remaining before the budget is exhausted."""
        return self._limit - self._consumed

    @property
    def session_id(self) -> str:
        """The session identifier this budget is associated with."""
        return self._session_id

    async def check_budget(self, estimated_tokens: int, role: str) -> None:
        """Pre-call estimation check.

        Raises BudgetExceededError if estimated_tokens exceeds remaining budget.
        Also emits a budget.exceeded event via the event emitter when the check fails.

        Args:
            estimated_tokens: The estimated token count for the upcoming call.
            role: The Role being requested (for error reporting).

        Raises:
            BudgetExceededError: If estimated_tokens > remaining budget.
        """
        if estimated_tokens > self.remaining:
            # Emit budget.exceeded event before raising
            if self._event_emitter is not None:
                event = Event.create(
                    type=EventType.BUDGET_EXCEEDED,
                    session_id=self._session_id,
                    source="session_budget",
                    correlation_id=self._session_id,
                    payload={
                        "role": role,
                        "estimated_tokens": estimated_tokens,
                        "remaining": self.remaining,
                        "limit": self._limit,
                        "consumed": self._consumed,
                    },
                    event_id=str(uuid.uuid4()),
                )
                await self._event_emitter(event)

            raise BudgetExceededError(
                role=role,
                estimated_tokens=estimated_tokens,
                remaining=self.remaining,
            )

    def charge(self, actual_tokens: int) -> None:
        """Post-call charge: add actual token usage to consumed amount.

        Called by the Model Router after a model call completes successfully.

        Args:
            actual_tokens: The actual number of tokens used by the call.

        Raises:
            ValueError: If actual_tokens is negative.
        """
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be non-negative")
        self._consumed += actual_tokens

    def summary(self) -> dict[str, Any]:
        """Return budget summary for the RuntimeInspector.

        Returns a dict with limit, consumed, and remaining for status reporting.
        Requirement 12.5: expose via RuntimeInspector without invoking AI.
        """
        return {
            "limit": self._limit,
            "consumed": self._consumed,
            "remaining": self.remaining,
        }
