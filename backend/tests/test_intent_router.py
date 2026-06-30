"""Unit tests for the intent router.

Validates requirements 2.4, 2.5, 2.6:
- Route status_query to RuntimeInspector without AI model
- Route build_intent into build workflow
- Treat interrupt intent as non-build when no build is in progress
"""

from __future__ import annotations

import pytest

from app.runtime.classifier import BuildState, ClassificationResult, IntentClass
from app.runtime.classifier.router import (
    IntentRouter,
    RoutingAction,
    RoutingResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _active_build() -> BuildState:
    """Build state with an active, non-paused build."""
    return BuildState(is_build_active=True, is_paused=False)


def _paused_build() -> BuildState:
    """Build state with an active, paused build."""
    return BuildState(is_build_active=True, is_paused=True)


def _no_build() -> BuildState:
    """Build state with no build in progress."""
    return BuildState(is_build_active=False, is_paused=False)


def _make_classification(intent: IntentClass, confidence: float = 1.0, rule: str | None = "test_rule") -> ClassificationResult:
    """Create a ClassificationResult for testing."""
    return ClassificationResult(intent=intent, confidence=confidence, matched_rule=rule)


def _mock_inspector(session_id: str) -> dict:
    """Mock RuntimeInspector callback that returns status data."""
    return {
        "session_id": session_id,
        "status": "running",
        "current_node": "execute",
        "tasks_completed": 3,
        "tasks_remaining": 2,
    }


async def _mock_async_inspector(session_id: str) -> dict:
    """Mock async RuntimeInspector callback."""
    return {
        "session_id": session_id,
        "status": "running",
        "current_node": "verify",
        "tasks_completed": 4,
        "tasks_remaining": 1,
    }


async def _mock_ai_classifier(message: str, session_id: str) -> dict:
    """Mock AI classifier callback."""
    return {
        "intent": "chitchat",
        "confidence": 0.85,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Requirement 2.4: Route status_query to RuntimeInspector without AI
# ---------------------------------------------------------------------------


class TestStatusQueryRouting:
    """Test that status_query is routed to RuntimeInspector without AI."""

    def test_status_query_calls_inspector(self) -> None:
        """status_query should invoke the RuntimeInspector callback."""
        router = IntentRouter(inspector_callback=_mock_inspector)
        classification = _make_classification(IntentClass.STATUS_QUERY)

        result = router.route(classification, session_id="sess-123")

        assert result.action == RoutingAction.STATUS_RESPONSE
        assert result.requires_ai is False
        assert result.data["session_id"] == "sess-123"
        assert result.data["status"] == "running"
        assert result.data["current_node"] == "execute"

    def test_status_query_does_not_require_ai(self) -> None:
        """status_query should never require AI invocation."""
        router = IntentRouter(inspector_callback=_mock_inspector)
        classification = _make_classification(IntentClass.STATUS_QUERY)

        result = router.route(classification, session_id="sess-456")

        assert result.requires_ai is False

    def test_status_query_without_inspector_returns_minimal(self) -> None:
        """status_query without inspector callback returns minimal response."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.STATUS_QUERY)

        result = router.route(classification, session_id="sess-789")

        assert result.action == RoutingAction.STATUS_RESPONSE
        assert result.requires_ai is False
        assert result.data["session_id"] == "sess-789"
        assert result.data["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_status_query_async_calls_async_inspector(self) -> None:
        """status_query async should use the async inspector callback."""
        router = IntentRouter(async_inspector_callback=_mock_async_inspector)
        classification = _make_classification(IntentClass.STATUS_QUERY)

        result = await router.route_async(classification, session_id="sess-async")

        assert result.action == RoutingAction.STATUS_RESPONSE
        assert result.requires_ai is False
        assert result.data["session_id"] == "sess-async"
        assert result.data["current_node"] == "verify"

    @pytest.mark.asyncio
    async def test_status_query_async_falls_back_to_sync(self) -> None:
        """status_query async falls back to sync callback when no async one."""
        router = IntentRouter(inspector_callback=_mock_inspector)
        classification = _make_classification(IntentClass.STATUS_QUERY)

        result = await router.route_async(classification, session_id="sess-fallback")

        assert result.action == RoutingAction.STATUS_RESPONSE
        assert result.data["session_id"] == "sess-fallback"
        assert result.data["current_node"] == "execute"


# ---------------------------------------------------------------------------
# Requirement 2.5: Route build_intent into build workflow
# ---------------------------------------------------------------------------


class TestBuildIntentRouting:
    """Test that build_intent is routed to the build workflow."""

    def test_build_intent_returns_build_workflow_action(self) -> None:
        """build_intent should return BUILD_WORKFLOW action."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.BUILD_INTENT)

        result = router.route(classification, message="build a REST API")

        assert result.action == RoutingAction.BUILD_WORKFLOW
        assert result.message == "build a REST API"
        assert result.requires_ai is False

    def test_build_intent_preserves_message(self) -> None:
        """build_intent should carry the original message for the workflow."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.BUILD_INTENT)

        result = router.route(classification, message="implement user authentication")

        assert result.action == RoutingAction.BUILD_WORKFLOW
        assert result.message == "implement user authentication"

    def test_build_intent_does_not_require_ai(self) -> None:
        """build_intent routing should not require AI."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.BUILD_INTENT)

        result = router.route(classification, message="add logging")

        assert result.requires_ai is False

    @pytest.mark.asyncio
    async def test_build_intent_async_returns_same_result(self) -> None:
        """build_intent async should return the same result as sync."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.BUILD_INTENT)

        result = await router.route_async(classification, message="fix the bug")

        assert result.action == RoutingAction.BUILD_WORKFLOW
        assert result.message == "fix the bug"
        assert result.requires_ai is False


# ---------------------------------------------------------------------------
# Requirement 2.6: Treat interrupt as non-build when no build in progress
# ---------------------------------------------------------------------------


class TestInterruptRoutingNoBuild:
    """Test that interrupt is treated as non-build when no build is in progress."""

    def test_interrupt_no_build_returns_non_build_message(self) -> None:
        """Interrupt with no active build should return NON_BUILD_MESSAGE."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.INTERRUPT)

        result = router.route(
            classification,
            message="interrupt",
            build_state=_no_build(),
        )

        assert result.action == RoutingAction.NON_BUILD_MESSAGE
        assert result.requires_ai is True

    def test_interrupt_active_build_returns_control_interrupt(self) -> None:
        """Interrupt with active build should return CONTROL_INTERRUPT."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.INTERRUPT)

        result = router.route(
            classification,
            message="interrupt",
            build_state=_active_build(),
        )

        assert result.action == RoutingAction.CONTROL_INTERRUPT
        assert result.message == "interrupt"

    def test_interrupt_paused_build_returns_control_interrupt(self) -> None:
        """Interrupt with paused build should return CONTROL_INTERRUPT."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.INTERRUPT)

        result = router.route(
            classification,
            message="interrupt",
            build_state=_paused_build(),
        )

        assert result.action == RoutingAction.CONTROL_INTERRUPT

    def test_interrupt_no_build_state_defaults_to_interrupt(self) -> None:
        """Interrupt with no build_state provided defaults to CONTROL_INTERRUPT.

        When build_state is None, the router cannot determine if a build is active,
        so it assumes the interrupt is valid (conservative behavior).
        """
        router = IntentRouter()
        classification = _make_classification(IntentClass.INTERRUPT)

        result = router.route(classification, message="interrupt", build_state=None)

        assert result.action == RoutingAction.CONTROL_INTERRUPT

    @pytest.mark.asyncio
    async def test_interrupt_no_build_async(self) -> None:
        """Interrupt with no build async should also return NON_BUILD_MESSAGE."""
        router = IntentRouter()
        classification = _make_classification(IntentClass.INTERRUPT)

        result = await router.route_async(
            classification,
            message="interrupt",
            build_state=_no_build(),
        )

        assert result.action == RoutingAction.NON_BUILD_MESSAGE
        assert result.requires_ai is True


# ---------------------------------------------------------------------------
# Control command routing (pause, resume, stop, redirect)
# ---------------------------------------------------------------------------


class TestControlCommandRouting:
    """Test routing of control commands."""

    def test_pause_routes_to_control_pause(self) -> None:
        router = IntentRouter()
        classification = _make_classification(IntentClass.PAUSE)

        result = router.route(classification, message="pause")

        assert result.action == RoutingAction.CONTROL_PAUSE
        assert result.message == "pause"

    def test_resume_routes_to_control_resume(self) -> None:
        router = IntentRouter()
        classification = _make_classification(IntentClass.RESUME)

        result = router.route(classification, message="resume")

        assert result.action == RoutingAction.CONTROL_RESUME
        assert result.message == "resume"

    def test_stop_routes_to_control_stop(self) -> None:
        router = IntentRouter()
        classification = _make_classification(IntentClass.STOP)

        result = router.route(classification, message="stop")

        assert result.action == RoutingAction.CONTROL_STOP
        assert result.message == "stop"

    def test_redirect_routes_to_control_redirect(self) -> None:
        router = IntentRouter()
        classification = _make_classification(IntentClass.REDIRECT)

        result = router.route(classification, message="redirect use React")

        assert result.action == RoutingAction.CONTROL_REDIRECT
        assert result.message == "redirect use React"


# ---------------------------------------------------------------------------
# AI classification fallback
# ---------------------------------------------------------------------------


class TestAIClassificationRouting:
    """Test routing of needs_ai_classification intent."""

    def test_needs_ai_returns_ai_classification_action(self) -> None:
        """needs_ai_classification should return AI_CLASSIFICATION action."""
        router = IntentRouter()
        classification = _make_classification(
            IntentClass.NEEDS_AI_CLASSIFICATION, confidence=0.0, rule=None
        )

        result = router.route(classification, message="hello there")

        assert result.action == RoutingAction.AI_CLASSIFICATION
        assert result.requires_ai is True
        assert result.message == "hello there"

    @pytest.mark.asyncio
    async def test_needs_ai_async_calls_ai_callback(self) -> None:
        """needs_ai_classification async should call the AI classifier callback."""
        router = IntentRouter(ai_classifier_callback=_mock_ai_classifier)
        classification = _make_classification(
            IntentClass.NEEDS_AI_CLASSIFICATION, confidence=0.0, rule=None
        )

        result = await router.route_async(
            classification, message="what's the weather?", session_id="sess-ai"
        )

        assert result.action == RoutingAction.AI_CLASSIFICATION
        assert result.data["intent"] == "chitchat"
        assert result.data["confidence"] == 0.85
        # AI has already classified, so requires_ai should be False
        assert result.requires_ai is False

    @pytest.mark.asyncio
    async def test_needs_ai_async_no_callback_returns_requires_ai(self) -> None:
        """needs_ai async without callback returns requires_ai=True."""
        router = IntentRouter()
        classification = _make_classification(
            IntentClass.NEEDS_AI_CLASSIFICATION, confidence=0.0, rule=None
        )

        result = await router.route_async(classification, message="hi")

        assert result.action == RoutingAction.AI_CLASSIFICATION
        assert result.requires_ai is True


# ---------------------------------------------------------------------------
# Classification unavailable
# ---------------------------------------------------------------------------


class TestClassificationUnavailable:
    """Test routing when classification is unavailable."""

    def test_unavailable_returns_unavailable_action(self) -> None:
        """classification_unavailable should return UNAVAILABLE action."""
        router = IntentRouter()
        classification = _make_classification(
            IntentClass.CLASSIFICATION_UNAVAILABLE, confidence=0.0, rule=None
        )

        result = router.route(classification, message="test")

        assert result.action == RoutingAction.UNAVAILABLE
        assert result.error == "No provider available to classify message"

    @pytest.mark.asyncio
    async def test_unavailable_async(self) -> None:
        """classification_unavailable async should also return UNAVAILABLE."""
        router = IntentRouter()
        classification = _make_classification(
            IntentClass.CLASSIFICATION_UNAVAILABLE, confidence=0.0, rule=None
        )

        result = await router.route_async(classification, message="test")

        assert result.action == RoutingAction.UNAVAILABLE
        assert result.error == "No provider available to classify message"


# ---------------------------------------------------------------------------
# End-to-end integration: classifier -> router
# ---------------------------------------------------------------------------


class TestClassifierToRouterIntegration:
    """Test the full flow from classifier output to router output."""

    def test_status_command_end_to_end(self) -> None:
        """'status' message -> classify -> route -> STATUS_RESPONSE."""
        from app.runtime.classifier import classify

        # Classify the message
        classification = classify("status", _no_build())
        assert classification.intent == IntentClass.STATUS_QUERY

        # Route the classification
        router = IntentRouter(inspector_callback=_mock_inspector)
        result = router.route(classification, session_id="e2e-sess")

        assert result.action == RoutingAction.STATUS_RESPONSE
        assert result.requires_ai is False
        assert result.data["status"] == "running"

    def test_build_command_end_to_end(self) -> None:
        """'build a REST API' -> classify -> route -> BUILD_WORKFLOW."""
        from app.runtime.classifier import classify

        classification = classify("build a REST API", _no_build())
        assert classification.intent == IntentClass.BUILD_INTENT

        router = IntentRouter()
        result = router.route(classification, message="build a REST API")

        assert result.action == RoutingAction.BUILD_WORKFLOW
        assert result.message == "build a REST API"

    def test_interrupt_no_build_end_to_end(self) -> None:
        """'interrupt' with no build -> classify -> route -> NON_BUILD (via NEEDS_AI).

        The classifier itself already adjusts interrupt to NEEDS_AI_CLASSIFICATION
        when no build is active. The router then handles it as AI_CLASSIFICATION.
        """
        from app.runtime.classifier import classify

        build_state = _no_build()
        classification = classify("interrupt", build_state, lambda r: True)

        # Classifier already adjusts to NEEDS_AI_CLASSIFICATION per Req 2.6
        assert classification.intent == IntentClass.NEEDS_AI_CLASSIFICATION

        router = IntentRouter()
        result = router.route(classification, message="interrupt", build_state=build_state)

        # Since classifier converted to NEEDS_AI, router returns AI_CLASSIFICATION
        assert result.action == RoutingAction.AI_CLASSIFICATION
        assert result.requires_ai is True

    def test_interrupt_active_build_end_to_end(self) -> None:
        """'interrupt' with active build -> classify -> route -> CONTROL_INTERRUPT."""
        from app.runtime.classifier import classify

        build_state = _active_build()
        classification = classify("interrupt", build_state)

        assert classification.intent == IntentClass.INTERRUPT

        router = IntentRouter()
        result = router.route(classification, message="interrupt", build_state=build_state)

        assert result.action == RoutingAction.CONTROL_INTERRUPT
