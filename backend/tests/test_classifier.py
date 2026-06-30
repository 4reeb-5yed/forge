"""Unit tests for the fast-path deterministic intent classifier.

Validates requirements 2.1, 2.2, 2.3, 2.6, 2.7, 2.8.
"""

from __future__ import annotations

import pytest

from app.runtime.classifier import (
    BuildState,
    ClassificationResult,
    IntentClass,
    classify,
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


def _registry_available(role: str) -> bool:
    """Registry checker that says all roles are available."""
    return True


def _registry_unavailable(role: str) -> bool:
    """Registry checker that says Interrupt_Handler is unavailable."""
    return False


# ---------------------------------------------------------------------------
# Requirement 2.2: Fast-path matching for each command type
# ---------------------------------------------------------------------------


class TestStopCommands:
    """Test that stop-related commands are classified as STOP."""

    @pytest.mark.parametrize("msg", [
        "stop",
        "Stop",
        "STOP",
        "  stop  ",
        "stop build",
        "stop building",
        "cancel",
        "abort",
        "halt",
        "kill",
    ])
    def test_stop_commands_during_active_build(self, msg: str) -> None:
        result = classify(msg, _active_build())
        assert result.intent == IntentClass.STOP
        assert result.confidence == 1.0
        assert result.matched_rule is not None


class TestPauseCommands:
    """Test that pause-related commands are classified as PAUSE."""

    @pytest.mark.parametrize("msg", [
        "pause",
        "Pause",
        "PAUSE",
        "pause build",
        "pause building",
        "wait",
        "hold",
        "hold on",
    ])
    def test_pause_commands_during_active_build(self, msg: str) -> None:
        result = classify(msg, _active_build())
        assert result.intent == IntentClass.PAUSE
        assert result.confidence == 1.0
        assert result.matched_rule is not None


class TestResumeCommands:
    """Test that resume-related commands are classified as RESUME."""

    @pytest.mark.parametrize("msg", [
        "resume",
        "Resume",
        "RESUME",
        "continue",
        "go",
        "proceed",
        "unpause",
    ])
    def test_resume_commands_during_paused_build(self, msg: str) -> None:
        result = classify(msg, _paused_build())
        assert result.intent == IntentClass.RESUME
        assert result.confidence == 1.0
        assert result.matched_rule is not None


class TestInterruptCommand:
    """Test that the interrupt command is classified as INTERRUPT."""

    def test_interrupt_during_active_build(self) -> None:
        result = classify("interrupt", _active_build())
        assert result.intent == IntentClass.INTERRUPT
        assert result.confidence == 1.0

    def test_interrupt_during_paused_build(self) -> None:
        result = classify("interrupt", _paused_build())
        assert result.intent == IntentClass.INTERRUPT
        assert result.confidence == 1.0


class TestStatusQueryCommands:
    """Test that status-related commands are classified as STATUS_QUERY."""

    @pytest.mark.parametrize("msg", [
        "status",
        "Status",
        "STATUS",
        "status?",
        "what's the status",
        "what is the status",
        "how's it going",
        "how is the build going",
        "progress",
        "show status",
        "show me status",
        "where are we",
        "where are you",
    ])
    def test_status_queries(self, msg: str) -> None:
        # Status queries work regardless of build state
        result = classify(msg, _no_build())
        assert result.intent == IntentClass.STATUS_QUERY
        assert result.confidence == 1.0
        assert result.matched_rule is not None

    def test_status_query_during_active_build(self) -> None:
        result = classify("status", _active_build())
        assert result.intent == IntentClass.STATUS_QUERY


class TestRedirectCommands:
    """Test that redirect commands are classified as REDIRECT."""

    @pytest.mark.parametrize("msg", [
        "redirect use React instead",
        "change direction use TypeScript",
        "instead, use PostgreSQL",
    ])
    def test_redirect_during_active_build(self, msg: str) -> None:
        result = classify(msg, _active_build())
        assert result.intent == IntentClass.REDIRECT
        assert result.confidence == 1.0
        assert result.matched_rule is not None


class TestBuildIntentCommands:
    """Test that build-related commands are classified as BUILD_INTENT."""

    @pytest.mark.parametrize("msg", [
        "build a REST API",
        "create user authentication",
        "implement pagination",
        "add logging",
        "fix the login bug",
        "refactor the database layer",
        "update the config module",
        "delete the old endpoint",
        "remove deprecated functions",
        "document the API",
        "analyze the codebase",
    ])
    def test_build_intent_commands(self, msg: str) -> None:
        result = classify(msg, _no_build())
        assert result.intent == IntentClass.BUILD_INTENT
        assert result.confidence == 1.0
        assert result.matched_rule is not None


# ---------------------------------------------------------------------------
# Requirement 2.1: Classify into exactly one intent class
# ---------------------------------------------------------------------------


class TestSingleIntentClassification:
    """Verify that each message maps to exactly one intent."""

    def test_stop_not_pause(self) -> None:
        """Stop takes priority over any other interpretation."""
        result = classify("stop", _active_build())
        assert result.intent == IntentClass.STOP

    def test_message_produces_single_result(self) -> None:
        """Calling classify always returns exactly one ClassificationResult."""
        result = classify("hello world", _no_build(), _registry_available)
        assert isinstance(result, ClassificationResult)
        assert isinstance(result.intent, IntentClass)


# ---------------------------------------------------------------------------
# Requirement 2.3: Fallback to Interrupt_Handler role (NEEDS_AI_CLASSIFICATION)
# ---------------------------------------------------------------------------


class TestFallbackToAI:
    """Test that unmatched messages fall back to AI classification."""

    @pytest.mark.parametrize("msg", [
        "hello",
        "what does this project do?",
        "can you explain the architecture?",
        "I'm confused about the tests",
        "thanks!",
        "nice work",
    ])
    def test_unmatched_messages_need_ai(self, msg: str) -> None:
        result = classify(msg, _no_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION
        assert result.confidence == 0.0
        assert result.matched_rule is None

    def test_empty_message_needs_ai(self) -> None:
        result = classify("", _no_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION

    def test_whitespace_only_needs_ai(self) -> None:
        result = classify("   ", _no_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION


# ---------------------------------------------------------------------------
# Requirement 2.8: classification-unavailable when Interrupt_Handler role missing
# ---------------------------------------------------------------------------


class TestClassificationUnavailable:
    """Test classification-unavailable when no AI provider can serve."""

    def test_unavailable_when_registry_says_no(self) -> None:
        result = classify("hello there", _no_build(), _registry_unavailable)
        assert result.intent == IntentClass.CLASSIFICATION_UNAVAILABLE
        assert result.confidence == 0.0

    def test_matched_commands_work_without_registry(self) -> None:
        """Rules-based matches don't depend on the registry checker."""
        result = classify("stop", _active_build(), _registry_unavailable)
        assert result.intent == IntentClass.STOP
        assert result.confidence == 1.0

    def test_no_registry_checker_defaults_to_needs_ai(self) -> None:
        """When no registry_checker is provided, assume AI is available."""
        result = classify("hello", _no_build(), None)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION


# ---------------------------------------------------------------------------
# Requirement 2.6: Build-state context adjustments
# ---------------------------------------------------------------------------


class TestBuildStateContext:
    """Test that build state affects classification of control commands."""

    def test_interrupt_without_build_falls_through(self) -> None:
        """Interrupt is not valid when no build is active (Req 2.6)."""
        result = classify("interrupt", _no_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION

    def test_pause_without_build_falls_through(self) -> None:
        """Pause is not meaningful without an active build."""
        result = classify("pause", _no_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION

    def test_resume_without_paused_build_falls_through(self) -> None:
        """Resume is not meaningful without a paused build."""
        result = classify("resume", _no_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION

    def test_resume_during_active_not_paused_falls_through(self) -> None:
        """Resume is not meaningful if build is active but not paused."""
        result = classify("resume", _active_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION

    def test_stop_without_build_falls_through(self) -> None:
        """Stop is not meaningful without an active build."""
        result = classify("stop", _no_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION

    def test_redirect_without_build_becomes_build_intent(self) -> None:
        """Redirect without a build becomes a build intent."""
        result = classify("redirect use React instead", _no_build())
        assert result.intent == IntentClass.BUILD_INTENT

    def test_status_query_works_regardless_of_build_state(self) -> None:
        """Status query is always valid regardless of build state."""
        assert classify("status", _no_build()).intent == IntentClass.STATUS_QUERY
        assert classify("status", _active_build()).intent == IntentClass.STATUS_QUERY
        assert classify("status", _paused_build()).intent == IntentClass.STATUS_QUERY

    def test_build_intent_works_regardless_of_build_state(self) -> None:
        """Build commands are valid regardless of build state."""
        assert classify("build something", _no_build()).intent == IntentClass.BUILD_INTENT
        assert classify("build something", _active_build()).intent == IntentClass.BUILD_INTENT


# ---------------------------------------------------------------------------
# Requirement 2.7: Determinism guarantee
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Verify that same message + same state = same result, always."""

    def test_repeated_classification_is_identical(self) -> None:
        """Calling classify 100 times with same inputs produces same output."""
        msg = "stop"
        state = _active_build()
        first_result = classify(msg, state)
        for _ in range(100):
            result = classify(msg, state)
            assert result == first_result

    def test_determinism_for_unmatched_messages(self) -> None:
        """Unmatched messages produce consistent fallback results."""
        msg = "what time is it?"
        state = _no_build()
        first_result = classify(msg, state, _registry_available)
        for _ in range(50):
            result = classify(msg, state, _registry_available)
            assert result == first_result

    def test_determinism_across_whitespace_variants(self) -> None:
        """Different whitespace normalization produces same result."""
        state = _active_build()
        r1 = classify("stop", state)
        r2 = classify("  stop  ", state)
        r3 = classify("STOP", state)
        r4 = classify(" Stop ", state)
        assert r1.intent == r2.intent == r3.intent == r4.intent == IntentClass.STOP

    def test_different_states_can_produce_different_results(self) -> None:
        """Same message with different states may produce different intents."""
        msg = "stop"
        active_result = classify(msg, _active_build())
        inactive_result = classify(msg, _no_build(), _registry_available)
        # With active build, stop is STOP; without, it falls through
        assert active_result.intent == IntentClass.STOP
        assert inactive_result.intent == IntentClass.NEEDS_AI_CLASSIFICATION


# ---------------------------------------------------------------------------
# Requirement 2.2: No AI model invoked for matched commands
# ---------------------------------------------------------------------------


class TestNoAIForMatchedCommands:
    """Verify that matched commands return with confidence 1.0 and a rule name.

    Confidence 1.0 + non-None matched_rule signals rules-based classification
    (no AI was needed). The caller can verify no model was invoked.
    """

    def test_matched_commands_have_full_confidence(self) -> None:
        commands = ["stop", "pause", "resume", "status", "build something"]
        for cmd in commands:
            result = classify(cmd, _paused_build())
            if result.matched_rule is not None:
                assert result.confidence == 1.0

    def test_fallback_has_zero_confidence(self) -> None:
        result = classify("tell me a joke", _no_build(), _registry_available)
        assert result.confidence == 0.0
        assert result.matched_rule is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_case_insensitive_matching(self) -> None:
        """All matching is case-insensitive."""
        assert classify("STOP", _active_build()).intent == IntentClass.STOP
        assert classify("Pause", _active_build()).intent == IntentClass.PAUSE
        assert classify("RESUME", _paused_build()).intent == IntentClass.RESUME
        assert classify("STATUS", _no_build()).intent == IntentClass.STATUS_QUERY

    def test_extra_whitespace_is_normalized(self) -> None:
        """Leading, trailing, and internal whitespace is normalized."""
        assert classify("  stop  ", _active_build()).intent == IntentClass.STOP
        assert classify("hold  on", _active_build()).intent == IntentClass.PAUSE
        assert classify("  show   me   status  ", _no_build()).intent == IntentClass.STATUS_QUERY

    def test_partial_matches_do_not_trigger(self) -> None:
        """Partial word matches should not trigger rules."""
        # "stopping" should not match "stop" (anchored regex)
        result = classify("stopping the build", _active_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION

    def test_substring_commands_do_not_trigger(self) -> None:
        """Commands embedded in longer sentences should not trigger exact matches."""
        result = classify("please stop doing that thing", _active_build(), _registry_available)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION


# ---------------------------------------------------------------------------
# Requirement 2.8: Build state preservation on classification-unavailable
# ---------------------------------------------------------------------------


class TestBuildStatePreservation:
    """Verify classification-unavailable does not mutate build state (Req 2.8)."""

    def test_classification_unavailable_preserves_active_build_state(self) -> None:
        """Build state should remain unchanged when classification is unavailable."""
        state = _active_build()
        original_active = state.is_build_active
        original_paused = state.is_paused

        result = classify("unknown message", state, _registry_unavailable)

        assert result.intent == IntentClass.CLASSIFICATION_UNAVAILABLE
        # Build state is frozen dataclass — verify it wasn't replaced
        assert state.is_build_active == original_active
        assert state.is_paused == original_paused

    def test_classification_unavailable_preserves_paused_build_state(self) -> None:
        """Paused build state should remain unchanged on classification-unavailable."""
        state = _paused_build()
        result = classify("some random message", state, _registry_unavailable)

        assert result.intent == IntentClass.CLASSIFICATION_UNAVAILABLE
        assert state.is_build_active is True
        assert state.is_paused is True

    def test_selective_registry_checker_interrupt_handler_only(self) -> None:
        """Registry checker that only rejects interrupt_handler role."""
        def selective_checker(role: str) -> bool:
            return role != "interrupt_handler"

        result = classify("hello there", _no_build(), selective_checker)
        assert result.intent == IntentClass.CLASSIFICATION_UNAVAILABLE

    def test_selective_registry_checker_accepts_interrupt_handler(self) -> None:
        """Registry checker that accepts interrupt_handler still falls back to AI."""
        def accepts_handler(role: str) -> bool:
            return role == "interrupt_handler"

        result = classify("hello there", _no_build(), accepts_handler)
        assert result.intent == IntentClass.NEEDS_AI_CLASSIFICATION
