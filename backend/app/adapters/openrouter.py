"""OpenRouter AI Provider adapter.

Implements the AIProvider protocol for the OpenRouter API, providing
chat completions via httpx async HTTP calls.
"""

from __future__ import annotations

import os
import logging
from typing import Any, AsyncIterator, Callable, Awaitable

import httpx

from app.shared import Health, PermanentError

logger = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    """OpenRouter AI provider using httpx.AsyncClient.

    Reads OPENROUTER_API_KEY from environment. Raises PermanentError on
    auth failures (401/403), RuntimeError on transient errors (429, 5xx).
    """

    name: str = "openrouter"

    def __init__(self, *, api_key: str | None = None, timeout: float = 60.0) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _classify_error(self, response: httpx.Response) -> None:
        """Classify HTTP errors and raise appropriate exceptions."""
        status = response.status_code
        if status in (401, 403):
            raise PermanentError(
                f"Authentication failed: HTTP {status}",
                provider="openrouter",
                error_type="auth_failure",
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise RuntimeError(
                f"Rate limited (429). Retry-After: {retry_after}"
            )
        if status >= 500:
            raise RuntimeError(
                f"Server error: HTTP {status} - {response.text[:200]}"
            )

    async def complete(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> str:
        """Complete a prompt and return the full response text.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            model: The model identifier (e.g. 'anthropic/claude-sonnet-4-20250514').
            **kwargs: Additional parameters forwarded to the API.

        Returns:
            The completion text from the model.
        """
        client = self._get_client()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        # Default max_tokens to prevent excessive token requests
        if "max_tokens" not in payload:
            payload["max_tokens"] = 4096

        response = await client.post(
            f"{BASE_URL}/chat/completions",
            headers=self._headers(),
            json=payload,
        )

        if response.status_code != 200:
            self._classify_error(response)

        data = response.json()
        
        # OpenRouter sometimes returns 200 with an error body
        if "error" in data:
            error_msg = data["error"].get("message", str(data["error"]))
            error_code = data["error"].get("code", 500)
            if error_code in (401, 403):
                raise PermanentError(
                    f"Authentication failed: {error_msg}",
                    provider="openrouter",
                    error_type="auth_failure",
                )
            raise RuntimeError(f"OpenRouter error: {error_msg}")

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            # Log the unexpected response for debugging
            logger.error("Unexpected OpenRouter response format: %s", str(data)[:500])
            raise RuntimeError(
                f"Unexpected response format from OpenRouter: {exc}. "
                f"Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
            ) from exc

    async def stream(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> AsyncIterator[str]:
        """Stream tokens from a prompt completion.

        Args:
            messages: List of message dicts.
            model: The model identifier.
            **kwargs: Additional parameters.

        Yields:
            Token strings as they arrive.
        """
        client = self._get_client()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            **kwargs,
        }

        async with client.stream(
            "POST",
            f"{BASE_URL}/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as response:
            if response.status_code != 200:
                await response.aread()
                self._classify_error(response)

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str.strip() == "[DONE]":
                    break
                import json
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

    async def health_check(self) -> Health:
        """Check provider connectivity by hitting the models endpoint."""
        try:
            client = self._get_client()
            response = await client.get(
                f"{BASE_URL}/models",
                headers=self._headers(),
            )
            if response.status_code == 200:
                return Health.healthy()
            return Health.unhealthy(f"HTTP {response.status_code}")
        except Exception as exc:
            return Health.unhealthy(str(exc))

    def as_call_adapter(self) -> Callable[..., Awaitable[str]]:
        """Return a closure matching the ModelCallAdapter signature.

        The returned callable has signature:
            async (provider: str, model: str, messages: list, **kwargs) -> str
        
        Strips any kwargs not supported by the OpenRouter API before forwarding.
        """
        # Keys that the OpenRouter API accepts (subset of OpenAI params)
        _ALLOWED_PARAMS = {
            "temperature", "top_p", "max_tokens", "stop", "presence_penalty",
            "frequency_penalty", "logit_bias", "n", "stream", "response_format",
            "seed", "tools", "tool_choice",
        }

        async def _adapter(provider: str, model: str, messages: list, **kwargs: Any) -> str:
            # Only pass API-compatible params; strip internal ones like estimated_tokens
            api_kwargs = {k: v for k, v in kwargs.items() if k in _ALLOWED_PARAMS}
            return await self.complete(messages=messages, model=model, **api_kwargs)

        return _adapter

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
