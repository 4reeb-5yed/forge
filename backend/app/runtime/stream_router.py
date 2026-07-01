"""Streaming Model Router - Real-time token streaming for AI completions.

Extends the ModelRouter to support streaming responses, emitting TOKEN events
for each token received. This enables real-time display of AI output in the frontend.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator, Callable

from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)


class StreamRouter:
    """Streaming wrapper for AI model completions.

    Wraps the ModelRouter's call_adapter to support streaming responses.
    Emits TOKEN events for each token received, enabling real-time display.
    """

    def __init__(
        self,
        model_router: Any,
        event_emitter: Callable | None = None,
    ) -> None:
        """Initialize the StreamRouter.

        Args:
            model_router: The ModelRouter instance to wrap.
            event_emitter: Optional async callable that emits Events.
        """
        self._router = model_router
        self._event_emitter = event_emitter
        self._token_buffer: dict[str, list[str]] = {}  # session_id -> tokens

    async def route_stream(
        self,
        role: Any,
        messages: list[dict[str, Any]],
        session_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Route a streaming completion request.

        Args:
            role: The Role to resolve.
            messages: The messages to complete.
            session_id: The session ID for event emission.
            **kwargs: Additional arguments passed to the call adapter.

        Yields:
            dict with 'token' key for each token received.
        """
        # Get the chain and call adapter
        chain = self._router._get_chain(role)
        
        for entry in chain:
            # Check registry availability
            if not self._router._registry_checker(entry.provider):
                continue

            # Check circuit breaker
            breaker = self._router._get_breaker(entry.provider)
            if breaker.state.value == "open":  # CircuitBreakerState.OPEN
                continue

            # Try streaming
            try:
                async for token_data in self._stream_from_provider(
                    entry.provider, entry.model, messages, session_id, **kwargs
                ):
                    yield token_data
                # If we got here, streaming succeeded
                breaker.record_success()
                return

            except Exception as exc:
                logger.warning(
                    "Streaming failed for provider '%s': %s",
                    entry.provider, exc
                )
                breaker.record_failure()

        # All providers exhausted
        raise Exception(f"No providers available for role {role}")

    async def _stream_from_provider(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        session_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream tokens from a specific provider.

        Args:
            provider: The provider name.
            model: The model name.
            messages: The messages to complete.
            session_id: The session ID.
            **kwargs: Additional arguments.

        Yields:
            dict with 'token' key for each token.
        """
        # Get the call adapter from the router
        call_adapter = self._router._call_adapter

        # Try to get streaming method
        try:
            # Check if adapter supports streaming
            if hasattr(call_adapter, '__self__'):
                adapter = call_adapter.__self__
                if hasattr(adapter, 'stream'):
                    # Provider has streaming support
                    async for token in adapter.stream(provider, model, messages, **kwargs):
                        await self._emit_token_event(token, session_id)
                        yield {"token": token, "done": False}
                    yield {"token": "", "done": True}
                    return
        except Exception:
            pass

        # Fall back to non-streaming with chunking
        try:
            result = await call_adapter(provider, model, messages, **kwargs)
            # Chunk the result into tokens (words for simplicity)
            words = result.split()
            for i, word in enumerate(words):
                token = word + (" " if i < len(words) - 1 else "")
                await self._emit_token_event(token, session_id)
                yield {"token": token, "done": False}
            yield {"token": "", "done": True}
        except Exception as exc:
            logger.error("Non-streaming fallback failed: %s", exc)
            raise

    async def _emit_token_event(self, token: str, session_id: str) -> None:
        """Emit a TOKEN event for real-time streaming.

        Args:
            token: The token that was received.
            session_id: The session ID.
        """
        if self._event_emitter is None:
            return

        try:
            event = Event.create(
                type=EventType.TOKEN,
                session_id=session_id,
                source="stream_router",
                payload={
                    "token": token,
                    "timestamp": str(uuid.uuid4()),
                },
                correlation_id=session_id,
                event_id=str(uuid.uuid4()),
            )
            await self._event_emitter(event)
        except Exception as exc:
            logger.warning("Failed to emit TOKEN event: %s", exc)


# Global stream router instance
_stream_router: StreamRouter | None = None


def get_stream_router() -> StreamRouter | None:
    """Get the global stream router instance."""
    return _stream_router


def set_stream_router(router: StreamRouter) -> None:
    """Set the global stream router instance."""
    global _stream_router
    _stream_router = router
