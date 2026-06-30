"""Fast-path deterministic intent classifier for the Forge Runtime.

Implements R10 from the architecture review: a rules-based classifier for
interrupt, pause, resume, stop, status, and structured commands. Matched
commands never invoke an AI model. Unmatched natural language falls back to
the Interrupt_Handler role. If that role is unavailable, returns
classification-unavailable.

Requirements: 2.1, 2.2, 2.3, 2.6, 2.7, 2.8
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class IntentClass(str, Enum):
    """Closed set of intent classes produced by the classifier.

    Each message is classified into exactly one of these.
    """

    INTERRUPT = "interrupt"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    STATUS_QUERY = "status_query"
    BUILD_INTENT = "build_intent"
    REDIRECT = "redirect"
    NEEDS_AI_CLASSIFICATION = "needs_ai_classification"
    CLASSIFICATION_UNAVAILABLE = "classification_unavailable"


@dataclass(frozen=True)
class BuildState:
    """Minimal build state needed for intent classification.

    The classifier uses this to contextualize commands (e.g., interrupt
    is only valid when a build is active).
    """

    is_build_active: bool = False
    is_paused: bool = False


@dataclass(frozen=True)
class ClassificationResult:
    """Result of intent classification.

    Attributes:
        intent: The classified intent class.
        confidence: 1.0 for rules-based matches, 0.0 for fallback.
        matched_rule: Name of the matched rule, or None for fallback.
    """

    intent: IntentClass
    confidence: float = 1.0
    matched_rule: str | None = None


# Type alias for a callable that checks if a given role is available
# in the capability registry. Returns True if the role can be served.
RegistryChecker = Callable[[str], bool]


# ---------------------------------------------------------------------------
# Rules table
# ---------------------------------------------------------------------------

# Each rule is a tuple of (pattern, intent_class, rule_name).
# Patterns are compiled regexes matched against the normalized message.
# Rules are evaluated in priority order — first match wins.
# This guarantees determinism: same message = same first-match = same intent.

_RULES: list[tuple[re.Pattern[str], IntentClass, str]] = [
    # Stop commands (highest priority — safety-critical)
    (re.compile(r"^stop$"), IntentClass.STOP, "exact_stop"),
    (re.compile(r"^stop\s+build(ing)?$"), IntentClass.STOP, "stop_build"),
    (re.compile(r"^cancel$"), IntentClass.STOP, "exact_cancel"),
    (re.compile(r"^abort$"), IntentClass.STOP, "exact_abort"),
    (re.compile(r"^halt$"), IntentClass.STOP, "exact_halt"),
    (re.compile(r"^kill$"), IntentClass.STOP, "exact_kill"),
    # Pause commands
    (re.compile(r"^pause$"), IntentClass.PAUSE, "exact_pause"),
    (re.compile(r"^pause\s+build(ing)?$"), IntentClass.PAUSE, "pause_build"),
    (re.compile(r"^wait$"), IntentClass.PAUSE, "exact_wait"),
    (re.compile(r"^hold$"), IntentClass.PAUSE, "exact_hold"),
    (re.compile(r"^hold\s+on$"), IntentClass.PAUSE, "hold_on"),
    # Resume commands
    (re.compile(r"^resume$"), IntentClass.RESUME, "exact_resume"),
    (re.compile(r"^continue$"), IntentClass.RESUME, "exact_continue"),
    (re.compile(r"^go$"), IntentClass.RESUME, "exact_go"),
    (re.compile(r"^proceed$"), IntentClass.RESUME, "exact_proceed"),
    (re.compile(r"^unpause$"), IntentClass.RESUME, "exact_unpause"),
    # Interrupt (general interruption, not stop/pause/resume)
    (re.compile(r"^interrupt$"), IntentClass.INTERRUPT, "exact_interrupt"),
    # Status query commands
    (re.compile(r"^status$"), IntentClass.STATUS_QUERY, "exact_status"),
    (re.compile(r"^status\?$"), IntentClass.STATUS_QUERY, "status_question"),
    (re.compile(r"^what('s|\s+is)\s+(the\s+)?status"), IntentClass.STATUS_QUERY, "whats_status"),
    (re.compile(r"^how('s|\s+is)\s+(it|the\s+build)\s+going"), IntentClass.STATUS_QUERY, "hows_going"),
    (re.compile(r"^progress$"), IntentClass.STATUS_QUERY, "exact_progress"),
    (re.compile(r"^show\s+(me\s+)?status"), IntentClass.STATUS_QUERY, "show_status"),
    (re.compile(r"^where\s+are\s+(we|you)"), IntentClass.STATUS_QUERY, "where_are_we"),
    # Redirect commands (change direction mid-build)
    (
        re.compile(r"^redirect\s+(.+)$"),
        IntentClass.REDIRECT,
        "redirect_with_direction",
    ),
    (
        re.compile(r"^change\s+direction\s+(.+)$"),
        IntentClass.REDIRECT,
        "change_direction",
    ),
    (
        re.compile(r"^instead[,:]?\s+(.+)$"),
        IntentClass.REDIRECT,
        "instead_redirect",
    ),
    # Build intent commands (explicit build triggers)
    (re.compile(r"^build\s+(.+)$"), IntentClass.BUILD_INTENT, "build_command"),
    (re.compile(r"^create\s+(.+)$"), IntentClass.BUILD_INTENT, "create_command"),
    (re.compile(r"^implement\s+(.+)$"), IntentClass.BUILD_INTENT, "implement_command"),
    (re.compile(r"^add\s+(.+)$"), IntentClass.BUILD_INTENT, "add_command"),
    (re.compile(r"^fix\s+(.+)$"), IntentClass.BUILD_INTENT, "fix_command"),
    (re.compile(r"^refactor\s+(.+)$"), IntentClass.BUILD_INTENT, "refactor_command"),
    (re.compile(r"^update\s+(.+)$"), IntentClass.BUILD_INTENT, "update_command"),
    (re.compile(r"^delete\s+(.+)$"), IntentClass.BUILD_INTENT, "delete_command"),
    (re.compile(r"^remove\s+(.+)$"), IntentClass.BUILD_INTENT, "remove_command"),
    (re.compile(r"^document\s+(.+)$"), IntentClass.BUILD_INTENT, "document_command"),
    (re.compile(r"^analyze\s+(.+)$"), IntentClass.BUILD_INTENT, "analyze_command"),
]


def _normalize_message(message: str) -> str:
    """Normalize a message for pattern matching.

    Lowercases, strips leading/trailing whitespace, and collapses
    internal whitespace to single spaces. This ensures deterministic
    matching regardless of incidental formatting.
    """
    return re.sub(r"\s+", " ", message.strip().lower())


def classify(
    message: str,
    build_state: BuildState,
    registry_checker: RegistryChecker | None = None,
) -> ClassificationResult:
    """Classify a message into exactly one intent class.

    The classifier is purely deterministic: same message + same build state
    = same intent class, always. No AI model is invoked for matched commands.

    Args:
        message: The raw user message to classify.
        build_state: Current build state (active, paused, etc.).
        registry_checker: Optional callable that returns True if a given
            role name is available in the capability registry. Used to
            determine whether the Interrupt_Handler role can serve fallback
            classification.

    Returns:
        A ClassificationResult with the matched intent class, confidence,
        and the name of the matched rule (if any).

    Determinism guarantee (Requirement 2.7):
        For any fixed (message, build_state, registry_checker result), this
        function always returns the same ClassificationResult.
    """
    normalized = _normalize_message(message)

    # Empty messages cannot be classified
    if not normalized:
        return _fallback_result(registry_checker)

    # Walk rules in priority order — first match wins
    for pattern, intent, rule_name in _RULES:
        if pattern.match(normalized):
            # Apply build-state context adjustments (Requirement 2.6)
            adjusted_intent = _apply_build_state_context(intent, build_state)
            return ClassificationResult(
                intent=adjusted_intent,
                confidence=1.0,
                matched_rule=rule_name,
            )

    # No rule matched — fall back to AI classification
    return _fallback_result(registry_checker)


def _apply_build_state_context(intent: IntentClass, build_state: BuildState) -> IntentClass:
    """Apply build-state context to adjust the classified intent.

    Per Requirement 2.6: while no build is in progress, if an interrupt
    intent is classified, treat the message as a non-build message (falls
    through to needs_ai_classification for the LLM to handle as chitchat/help).

    Pause/resume/stop/redirect also require an active build to be meaningful
    as control commands. Without an active build, they are treated as potential
    build intents or need AI classification.
    """
    # Interrupt only valid mid-build
    if intent == IntentClass.INTERRUPT and not build_state.is_build_active:
        return IntentClass.NEEDS_AI_CLASSIFICATION

    # Pause only valid when build is active and not already paused
    if intent == IntentClass.PAUSE and not build_state.is_build_active:
        return IntentClass.NEEDS_AI_CLASSIFICATION

    # Resume only valid when build is paused
    if intent == IntentClass.RESUME and not build_state.is_paused:
        return IntentClass.NEEDS_AI_CLASSIFICATION

    # Stop only valid when build is active
    if intent == IntentClass.STOP and not build_state.is_build_active:
        return IntentClass.NEEDS_AI_CLASSIFICATION

    # Redirect only valid when build is active (paused or running)
    if intent == IntentClass.REDIRECT and not build_state.is_build_active:
        return IntentClass.BUILD_INTENT

    return intent


def _fallback_result(registry_checker: RegistryChecker | None) -> ClassificationResult:
    """Produce the fallback classification result.

    If a registry_checker is provided, verifies that the Interrupt_Handler
    role is available. If not available, returns classification-unavailable
    (Requirement 2.8).
    """
    if registry_checker is not None:
        if not registry_checker("interrupt_handler"):
            return ClassificationResult(
                intent=IntentClass.CLASSIFICATION_UNAVAILABLE,
                confidence=0.0,
                matched_rule=None,
            )

    return ClassificationResult(
        intent=IntentClass.NEEDS_AI_CLASSIFICATION,
        confidence=0.0,
        matched_rule=None,
    )
