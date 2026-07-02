"""Unit tests for the Recovery API endpoints.

Tests session recovery, checkpoint listing, and resume functionality.
"""

from __future__ import annotations

from app.api.recovery import recovery_router


class TestRecoveryRouter:
    """Tests for the recovery router structure."""

    def test_recovery_router_exists(self) -> None:
        """Recovery router is defined."""
        assert recovery_router is not None

    def test_recovery_router_has_prefix(self) -> None:
        """Recovery router has /recovery prefix."""
        assert recovery_router.prefix == "/recovery"

    def test_recovery_router_has_tags(self) -> None:
        """Recovery router has recovery tag."""
        assert "recovery" in recovery_router.tags

    def test_recovery_router_has_routes(self) -> None:
        """Recovery router has route definitions."""
        # The router should have routes defined
        routes = [r for r in recovery_router.routes]
        assert len(routes) >= 3  # At least list, get, and resume endpoints
