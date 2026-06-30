"""Intent Router — maps classified intents to runtime actions.

Accepts a ClassificationResult from the fast-path classifier and routes it
to the appropriate handler:
- status_query: calls RuntimeInspector (no AI model)
- build_intent: triggers the build workflow
- interrupt/pause/resume/stop/redirect: returns control-action routing results
- needs_ai_classification: invokes the Interrupt_Handler role via ModelRouter
- classification_unavailable: returns an error/unavailable result

Requirements: 2.4, 2.5, 2.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


from app.runtime.classifier import BuildState, ClassificationResult, IntentClass


# ---------------------------------------------------------------------------
# Routing action types
# ---------------------------------------------------------------------------


class RoutingAction(str, Enum):
    """The action category determined by the intent router.

    Each intent is mapped to exactly one routing action, which tells the
    caller what to do next.
    """

    STATUS_RESPONSE = "status_response"
    BUILD_WORKFLOW = "build_workflow"
    CONTROL_INTERRUPT = "control_interrupt"
    CONTROL_PAUSE = "control_pause"
    CONTROL_RESUME = "control_resume"
    CONTROL_STOP = "control_stop"
    CONTROL_REDIRECT = "control_redirect"
    AI_CLASSIFICATION = "ai_classification"
    UNAVAILABLE = "unavailable"
    NON_BUILD_MESSAGE = "non_build_message"


@dataclass(frozen=True)
class RoutingResult:
    """Result of intent routing — what action the runtime should take.

    Attributes:
        action: The routing action category.
        data: Any associated data (e.g., inspector response, AI classification).
        message: Original message text (useful for build workflow or AI routing).
        requires_ai: Whether this result still needs AI processing.
        error: Error message if action is UNAVAILABLE.
    """

    action: RoutingAction
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    requires_ai: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Callback type aliases
# ---------------------------------------------------------------------------

# Callback that invokes the RuntimeInspector and returns status data.
# Signature: (session_id) -> dict with status information
RuntimeInspectorCallback = Callable[[str], dict[str, Any]]

# Async version of the inspector callback
AsyncRuntimeInspectorCallback = Callable[[str], Awaitable[dict[str, Any]]]

# Callback to invoke the Interrupt_Handler role via ModelRouter for AI classification.
# Signature: (message, session_id) -> ClassificationResult or dict
AsyncAIClassifierCallback = Callable[[str, str], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Intent Router
# ---------------------------------------------------------------------------


class IntentRouter:
    """Routes classified intents to appropriate runtime actions.

    The router maps each IntentClass to a RoutingAction and, where applicable,
    invokes callbacks (e.g., RuntimeInspector for status queries). It does not
    perform the actual work — it returns a RoutingResult that tells the caller
    what action to take.

    Design decisions:
    - status_query: Calls the RuntimeInspector callback directly (no AI). Per Req 2.4.
    - build_intent: Returns a BUILD_WORKFLOW action for the caller to handle. Per Req 2.5.
    - interrupt (no build): Returns NON_BUILD_MESSAGE action. Per Req 2.6.
    - needs_ai_classification: Returns AI_CLASSIFICATION action for async processing.
    - classification_unavailable: Returns UNAVAILABLE with error.

    Requirements:
        2.4 — status_query answered via RuntimeInspector without AI
        2.5 — build_intent routed into build workflow
        2.6 — interrupt treated as non-build when no build in progress
    """

    def __init__(
        self,
        *,
        inspector_callback: RuntimeInspectorCallback | None = None,
        async_inspector_callback: AsyncRuntimeInspectorCallback | None = None,
        ai_classifier_callback: AsyncAIClassifierCallback | None = None,
    ) -> None:
        """Initialize the IntentRouter.

        Args:
            inspector_callback: Sync callback to get status from RuntimeInspector.
            async_inspector_callback: Async version of the inspector callback.
            ai_classifier_callback: Async callback for AI-based classification
                when the fast-path doesn't match.
        """
        self._inspector_callback = inspector_callback
        self._async_inspector_callback = async_inspector_callback
        self._ai_classifier_callback = ai_classifier_callback

    def route(
        self,
        classification: ClassificationResult,
        *,
        message: str = "",
        session_id: str = "",
        build_state: BuildState | None = None,
    ) -> RoutingResult:
        """Route a classified intent to the appropriate action.

        This is the synchronous routing entry point. For status_query, it
        calls the sync RuntimeInspector callback. For intents requiring
        async processing (AI classification), it returns a result indicating
        the caller should use route_async().

        Args:
            classification: The ClassificationResult from the classifier.
            message: The original user message.
            session_id: The current session identifier.
            build_state: Current build state (used for context).

        Returns:
            A RoutingResult indicating what action to take.
        """
        intent = classification.intent

        if intent == IntentClass.STATUS_QUERY:
            return self._route_status_query(session_id)

        if intent == IntentClass.BUILD_INTENT:
            return self._route_build_intent(message)

        if intent == IntentClass.INTERRUPT:
            return self._route_interrupt(message, build_state)

        if intent == IntentClass.PAUSE:
            return RoutingResult(
                action=RoutingAction.CONTROL_PAUSE,
                message=message,
            )

        if intent == IntentClass.RESUME:
            return RoutingResult(
                action=RoutingAction.CONTROL_RESUME,
                message=message,
            )

        if intent == IntentClass.STOP:
            return RoutingResult(
                action=RoutingAction.CONTROL_STOP,
                message=message,
            )

        if intent == IntentClass.REDIRECT:
            return RoutingResult(
                action=RoutingAction.CONTROL_REDIRECT,
                message=message,
            )

        if intent == IntentClass.NEEDS_AI_CLASSIFICATION:
            return RoutingResult(
                action=RoutingAction.AI_CLASSIFICATION,
                message=message,
                requires_ai=True,
            )

        if intent == IntentClass.CLASSIFICATION_UNAVAILABLE:
            return RoutingResult(
                action=RoutingAction.UNAVAILABLE,
                message=message,
                error="No provider available to classify message",
            )

        # Fallback for any unknown intent (defensive)
        return RoutingResult(
            action=RoutingAction.UNAVAILABLE,
            message=message,
            error=f"Unknown intent class: {intent}",
        )

    async def route_async(
        self,
        classification: ClassificationResult,
        *,
        message: str = "",
        session_id: str = "",
        build_state: BuildState | None = None,
    ) -> RoutingResult:
        """Route a classified intent to the appropriate action (async version).

        For status_query, uses the async inspector callback if available.
        For needs_ai_classification, invokes the AI classifier callback.

        Args:
            classification: The ClassificationResult from the classifier.
            message: The original user message.
            session_id: The current session identifier.
            build_state: Current build state (used for context).

        Returns:
            A RoutingResult indicating what action to take.
        """
        intent = classification.intent

        if intent == IntentClass.STATUS_QUERY:
            return await self._route_status_query_async(session_id)

        if intent == IntentClass.BUILD_INTENT:
            return self._route_build_intent(message)

        if intent == IntentClass.INTERRUPT:
            return self._route_interrupt(message, build_state)

        if intent == IntentClass.PAUSE:
            return RoutingResult(
                action=RoutingAction.CONTROL_PAUSE,
                message=message,
            )

        if intent == IntentClass.RESUME:
            return RoutingResult(
                action=RoutingAction.CONTROL_RESUME,
                message=message,
            )

        if intent == IntentClass.STOP:
            return RoutingResult(
                action=RoutingAction.CONTROL_STOP,
                message=message,
            )

        if intent == IntentClass.REDIRECT:
            return RoutingResult(
                action=RoutingAction.CONTROL_REDIRECT,
                message=message,
            )

        if intent == IntentClass.NEEDS_AI_CLASSIFICATION:
            return await self._route_ai_classification(message, session_id)

        if intent == IntentClass.CLASSIFICATION_UNAVAILABLE:
            return RoutingResult(
                action=RoutingAction.UNAVAILABLE,
                message=message,
                error="No provider available to classify message",
            )

        # Fallback for any unknown intent (defensive)
        return RoutingResult(
            action=RoutingAction.UNAVAILABLE,
            message=message,
            error=f"Unknown intent class: {intent}",
        )

    # ------------------------------------------------------------------
    # Private routing methods
    # ------------------------------------------------------------------

    def _route_status_query(self, session_id: str) -> RoutingResult:
        """Route status_query to RuntimeInspector (no AI). Req 2.4."""
        if self._inspector_callback is not None:
            data = self._inspector_callback(session_id)
            return RoutingResult(
                action=RoutingAction.STATUS_RESPONSE,
                data=data,
                requires_ai=False,
            )

        # No inspector callback configured — return minimal status response
        return RoutingResult(
            action=RoutingAction.STATUS_RESPONSE,
            data={"status": "unknown", "session_id": session_id},
            requires_ai=False,
        )

    async def _route_status_query_async(self, session_id: str) -> RoutingResult:
        """Route status_query using async inspector callback. Req 2.4."""
        if self._async_inspector_callback is not None:
            data = await self._async_inspector_callback(session_id)
            return RoutingResult(
                action=RoutingAction.STATUS_RESPONSE,
                data=data,
                requires_ai=False,
            )

        # Fall back to sync callback
        return self._route_status_query(session_id)

    def _route_build_intent(self, message: str) -> RoutingResult:
        """Route build_intent into the build workflow. Req 2.5."""
        return RoutingResult(
            action=RoutingAction.BUILD_WORKFLOW,
            message=message,
            requires_ai=False,
        )

    def _route_interrupt(
        self, message: str, build_state: BuildState | None
    ) -> RoutingResult:
        """Route interrupt intent, considering build state. Req 2.6.

        If no build is in progress, treat the interrupt as a non-build message
        rather than a control command. The classifier already handles this in
        _apply_build_state_context, but the router provides an additional guard
        in case a raw INTERRUPT classification reaches it without state adjustment.
        """
        # If build state indicates no active build, treat as non-build
        if build_state is not None and not build_state.is_build_active:
            return RoutingResult(
                action=RoutingAction.NON_BUILD_MESSAGE,
                message=message,
                requires_ai=True,
            )

        # Build is active — this is a legitimate interrupt
        return RoutingResult(
            action=RoutingAction.CONTROL_INTERRUPT,
            message=message,
        )

    async def _route_ai_classification(
        self, message: str, session_id: str
    ) -> RoutingResult:
        """Route to AI classifier for ambiguous natural language. Req 2.3."""
        if self._ai_classifier_callback is not None:
            ai_result = await self._ai_classifier_callback(message, session_id)
            return RoutingResult(
                action=RoutingAction.AI_CLASSIFICATION,
                data=ai_result,
                message=message,
                requires_ai=False,  # AI has already classified it
            )

        # No AI callback configured — return as-is for caller to handle
        return RoutingResult(
            action=RoutingAction.AI_CLASSIFICATION,
            message=message,
            requires_ai=True,
        )
