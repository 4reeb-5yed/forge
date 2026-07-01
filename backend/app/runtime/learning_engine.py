"""Learning Engine - Analyzes failure patterns and suggests improvements.

Analyzes recorded outcomes from the LearningRecorder to identify patterns
in failures and successes, and generates recommendations for improving
future builds.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Default analysis window (7 days)
DEFAULT_ANALYSIS_WINDOW_DAYS = int(os.environ.get("FORGE_LEARNING_WINDOW_DAYS", "7"))


@dataclass
class LearningPattern:
    """Identified pattern from historical outcomes."""
    pattern_type: str
    description: str
    frequency: int
    success_rate: float
    affected_tasks: list[str]
    recommendations: list[str]


@dataclass
class ModelPerformance:
    """Performance metrics for a specific model."""
    model_name: str
    total_calls: int
    successful_calls: int
    failed_calls: int
    average_latency_ms: float
    failure_reasons: dict[str, int]


@dataclass
class ProviderHealth:
    """Health metrics for a provider."""
    provider_name: str
    uptime_percentage: float
    average_latency_ms: float
    circuit_breaker_trips: int
    recommended_model: str | None


@dataclass
class LearningRecommendations:
    """Recommendations generated from learning analysis."""
    model_recommendations: list[dict[str, Any]]
    provider_recommendations: list[dict[str, Any]]
    task_patterns: list[dict[str, Any]]
    overall_health: str
    summary: str


class LearningEngine:
    """Analyzes historical outcomes to generate actionable recommendations.

    Processes outcomes from the LearningRecorder to identify:
    - Model performance patterns
    - Provider health issues
    - Task success/failure patterns
    - Retry effectiveness
    """

    def __init__(
        self,
        learning_store: Any | None = None,
        analysis_window_days: int = DEFAULT_ANALYSIS_WINDOW_DAYS,
    ) -> None:
        """Initialize the LearningEngine.

        Args:
            learning_store: The learning store for reading outcomes.
            analysis_window_days: Number of days to analyze (default: 7).
        """
        self._learning_store = learning_store
        self._analysis_window_days = analysis_window_days
        self._patterns_cache: list[LearningPattern] = []
        self._last_analysis: datetime | None = None

    async def analyze(self) -> LearningRecommendations:
        """Analyze historical outcomes and generate recommendations.

        Returns:
            LearningRecommendations with actionable insights.
        """
        if self._learning_store is None:
            return self._generate_default_recommendations()

        try:
            # Get all outcomes from the learning store
            outcomes = await self._get_all_outcomes()

            if not outcomes:
                return self._generate_no_data_recommendations()

            # Analyze patterns
            model_performance = self._analyze_model_performance(outcomes)
            provider_health = self._analyze_provider_health(outcomes)
            task_patterns = self._analyze_task_patterns(outcomes)

            # Generate recommendations
            recommendations = LearningRecommendations(
                model_recommendations=model_performance,
                provider_recommendations=provider_health,
                task_patterns=task_patterns,
                overall_health=self._calculate_overall_health(outcomes),
                summary=self._generate_summary(outcomes),
            )

            self._patterns_cache = task_patterns
            self._last_analysis = datetime.now(timezone.utc)

            return recommendations

        except Exception as exc:
            logger.warning("Learning analysis failed: %s", exc)
            return self._generate_error_recommendations(str(exc))

    async def _get_all_outcomes(self) -> list[dict[str, Any]]:
        """Get all outcomes from the learning store."""
        if self._learning_store is None:
            return []

        # Get outcomes from the last analysis window
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._analysis_window_days)

        # Get all outcomes (we'd need to iterate by session in a real implementation)
        all_outcomes = []
        try:
            if hasattr(self._learning_store, 'get_outcomes'):
                # This is a simplified version - in reality we'd iterate sessions
                outcomes = await self._learning_store.get_outcomes("")
                for outcome in outcomes:
                    created_at = outcome.get("created_at")
                    if isinstance(created_at, str):
                        try:
                            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                    if created_at and created_at >= cutoff:
                        all_outcomes.append(outcome)
        except Exception:
            pass

        return all_outcomes

    def _analyze_model_performance(
        self, outcomes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Analyze model performance from outcomes.

        Args:
            outcomes: List of outcome records.

        Returns:
            List of model performance recommendations.
        """
        # Group by model
        model_stats: dict[str, dict[str, Any]] = {}

        for outcome in outcomes:
            model_name = outcome.get("model_used") or outcome.get("data", {}).get("model", "unknown")
            if model_name not in model_stats:
                model_stats[model_name] = {
                    "total": 0,
                    "success": 0,
                    "failure": 0,
                    "latencies": [],
                }

            stats = model_stats[model_name]
            stats["total"] += 1

            status = outcome.get("outcome_status") or outcome.get("status", "unknown")
            if status in ("success", "completed"):
                stats["success"] += 1
            else:
                stats["failure"] += 1

            latency = outcome.get("data", {}).get("latency_ms", 0)
            if latency:
                stats["latencies"].append(latency)

        # Generate recommendations
        recommendations = []
        for model_name, stats in sorted(
            model_stats.items(), key=lambda x: x[1]["total"], reverse=True
        ):
            success_rate = stats["success"] / max(stats["total"], 1)
            avg_latency = sum(stats["latencies"]) / max(len(stats["latencies"]), 1)

            recommendation = {
                "model": model_name,
                "total_calls": stats["total"],
                "success_rate": round(success_rate * 100, 1),
                "average_latency_ms": round(avg_latency, 1),
                "recommendation": self._get_model_recommendation(success_rate, avg_latency),
            }
            recommendations.append(recommendation)

        return recommendations

    def _analyze_provider_health(
        self, outcomes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Analyze provider health from outcomes.

        Args:
            outcomes: List of outcome records.

        Returns:
            List of provider health recommendations.
        """
        # Group by provider
        provider_stats: dict[str, dict[str, Any]] = {}

        for outcome in outcomes:
            provider = outcome.get("provider") or outcome.get("data", {}).get("provider", "unknown")
            if provider not in provider_stats:
                provider_stats[provider] = {
                    "total": 0,
                    "success": 0,
                    "failure": 0,
                    "latencies": [],
                    "errors": [],
                }

            stats = provider_stats[provider]
            stats["total"] += 1

            status = outcome.get("outcome_status") or outcome.get("status", "unknown")
            if status in ("success", "completed"):
                stats["success"] += 1
            else:
                stats["failure"] += 1
                error = outcome.get("error") or outcome.get("data", {}).get("error", "unknown")
                stats["errors"].append(error)

            latency = outcome.get("data", {}).get("latency_ms", 0)
            if latency:
                stats["latencies"].append(latency)

        # Generate recommendations
        recommendations = []
        for provider, stats in sorted(
            provider_stats.items(), key=lambda x: x[1]["total"], reverse=True
        ):
            uptime = stats["success"] / max(stats["total"], 1) * 100
            avg_latency = sum(stats["latencies"]) / max(len(stats["latencies"]), 1)
            error_counts = Counter(stats["errors"])

            recommendation = {
                "provider": provider,
                "uptime_percentage": round(uptime, 2),
                "average_latency_ms": round(avg_latency, 1),
                "total_calls": stats["total"],
                "common_errors": dict(error_counts.most_common(5)),
                "health_status": self._get_health_status(uptime, avg_latency),
            }
            recommendations.append(recommendation)

        return recommendations

    def _analyze_task_patterns(
        self, outcomes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Analyze task success/failure patterns.

        Args:
            outcomes: List of outcome records.

        Returns:
            List of task pattern recommendations.
        """
        patterns = []

        # Group by task type
        task_stats: dict[str, dict[str, Any]] = {}

        for outcome in outcomes:
            task_id = outcome.get("task_id", "unknown")
            # Extract task type from task_id (e.g., "clarify", "plan", "execute")
            task_type = outcome.get("data", {}).get("task_type", "unknown")

            key = f"{task_type}:{task_id}"
            if key not in task_stats:
                task_stats[key] = {
                    "type": task_type,
                    "total": 0,
                    "success": 0,
                    "failure": 0,
                    "retry_count": 0,
                }

            stats = task_stats[key]
            stats["total"] += 1

            status = outcome.get("outcome_status") or outcome.get("status", "unknown")
            if status in ("success", "completed"):
                stats["success"] += 1
            else:
                stats["failure"] += 1

            retries = outcome.get("data", {}).get("retry_count", 0)
            stats["retry_count"] += retries

        # Generate patterns
        for task_key, stats in task_stats.items():
            if stats["total"] < 3:  # Skip tasks with too few samples
                continue

            success_rate = stats["success"] / max(stats["total"], 1)
            avg_retries = stats["retry_count"] / max(stats["total"], 1)

            pattern = {
                "task_type": stats["type"],
                "sample_count": stats["total"],
                "success_rate": round(success_rate * 100, 1),
                "average_retries": round(avg_retries, 1),
                "pattern_type": self._get_task_pattern_type(success_rate, avg_retries),
                "recommendation": self._get_task_recommendation(success_rate, avg_retries),
            }
            patterns.append(pattern)

        return sorted(patterns, key=lambda x: x["sample_count"], reverse=True)

    def _calculate_overall_health(self, outcomes: list[dict[str, Any]]) -> str:
        """Calculate overall system health from outcomes.

        Args:
            outcomes: List of outcome records.

        Returns:
            Health status string.
        """
        if not outcomes:
            return "unknown"

        success_count = sum(
            1 for o in outcomes
            if o.get("outcome_status") in ("success", "completed")
        )
        success_rate = success_count / len(outcomes)

        if success_rate >= 0.95:
            return "healthy"
        elif success_rate >= 0.85:
            return "degraded"
        elif success_rate >= 0.70:
            return "unhealthy"
        else:
            return "critical"

    def _generate_summary(self, outcomes: list[dict[str, Any]]) -> str:
        """Generate a human-readable summary.

        Args:
            outcomes: List of outcome records.

        Returns:
            Summary string.
        """
        total = len(outcomes)
        if total == 0:
            return "No historical data available for analysis."

        success_count = sum(
            1 for o in outcomes
            if o.get("outcome_status") in ("success", "completed")
        )
        success_rate = success_count / total

        return (
            f"Analysis of {total} outcomes over {self._analysis_window_days} days: "
            f"{success_rate:.1%} success rate. "
            f"{success_count} successful, {total - success_count} failed."
        )

    @staticmethod
    def _get_model_recommendation(success_rate: float, avg_latency: float) -> str:
        """Get recommendation for a model based on metrics."""
        if success_rate >= 0.95 and avg_latency < 5000:
            return "Excellent performance - continue using"
        elif success_rate >= 0.90:
            return "Good performance - monitor for issues"
        elif success_rate >= 0.80:
            return "Consider as fallback - primary model may be better"
        else:
            return "High failure rate - avoid for critical tasks"

    @staticmethod
    def _get_health_status(uptime: float, avg_latency: float) -> str:
        """Get health status for a provider."""
        if uptime >= 99 and avg_latency < 3000:
            return "healthy"
        elif uptime >= 95:
            return "degraded"
        elif uptime >= 90:
            return "unhealthy"
        else:
            return "critical"

    @staticmethod
    def _get_task_pattern_type(success_rate: float, avg_retries: float) -> str:
        """Get pattern type for a task."""
        if success_rate >= 0.95 and avg_retries < 0.5:
            return "reliable"
        elif success_rate >= 0.90:
            return "stable"
        elif avg_retries > 2:
            return "retry_heavy"
        else:
            return "unreliable"

    @staticmethod
    def _get_task_recommendation(success_rate: float, avg_retries: float) -> str:
        """Get recommendation for a task pattern."""
        if success_rate >= 0.95:
            return "Task type performs well - optimal configuration"
        elif avg_retries > 2:
            return "High retry count - consider breaking into subtasks"
        elif success_rate < 0.70:
            return "Low success rate - review requirements or approach"
        else:
            return "Room for improvement - consider optimization"

    def _generate_default_recommendations(self) -> LearningRecommendations:
        """Generate default recommendations when no data is available."""
        return LearningRecommendations(
            model_recommendations=[],
            provider_recommendations=[],
            task_patterns=[],
            overall_health="unknown",
            summary="No historical data available. Run some builds to collect data for analysis.",
        )

    def _generate_no_data_recommendations(self) -> LearningRecommendations:
        """Generate recommendations when no outcomes are found."""
        return LearningRecommendations(
            model_recommendations=[],
            provider_recommendations=[],
            task_patterns=[],
            overall_health="unknown",
            summary="No outcomes found in the analysis window. Ensure the learning recorder is functioning.",
        )

    def _generate_error_recommendations(self, error: str) -> LearningRecommendations:
        """Generate recommendations when analysis fails."""
        return LearningRecommendations(
            model_recommendations=[],
            provider_recommendations=[],
            task_patterns=[],
            overall_health="unknown",
            summary=f"Learning analysis encountered an error: {error}",
        )


# Global learning engine instance
_learning_engine: LearningEngine | None = None


def get_learning_engine() -> LearningEngine:
    """Get the global learning engine instance."""
    global _learning_engine
    if _learning_engine is None:
        _learning_engine = LearningEngine()
    return _learning_engine


def set_learning_engine(engine: LearningEngine) -> None:
    """Set the global learning engine instance."""
    global _learning_engine
    _learning_engine = engine
