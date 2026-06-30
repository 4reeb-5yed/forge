"""Unit tests for layer boundary enforcement.

Tests that the boundary checker correctly identifies violations when:
- A module imports across more than one layer boundary
- Application layer contains engineering logic
- Runtime layer contains HTTP/transport-specific logic
- Adapter layer contains business/orchestration logic

Also verifies that valid imports (same layer, adjacent layer, external)
are correctly permitted.

Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6
"""

from __future__ import annotations

import pytest

from app.boundaries import (
    BoundaryViolation,
    Layer,
    LAYER_ADJACENCY,
    check_all_boundaries,
    check_forbidden_imports,
    check_import_allowed,
    check_module_boundaries,
    classify_module,
    extract_imports_from_source,
)


# ---------------------------------------------------------------------------
# classify_module tests
# ---------------------------------------------------------------------------


class TestClassifyModule:
    """Tests for module-to-layer classification."""

    def test_application_layer_detection(self) -> None:
        """Modules under app.api are classified as APPLICATION."""
        assert classify_module("app.api") == Layer.APPLICATION
        assert classify_module("app.api.auth") == Layer.APPLICATION
        assert classify_module("app.api.endpoints") == Layer.APPLICATION

    def test_runtime_layer_detection(self) -> None:
        """Modules under app.runtime are classified as RUNTIME."""
        assert classify_module("app.runtime") == Layer.RUNTIME
        assert classify_module("app.runtime.events.models") == Layer.RUNTIME
        assert classify_module("app.runtime.session") == Layer.RUNTIME
        assert classify_module("app.runtime.inspector") == Layer.RUNTIME

    def test_adapter_layer_detection(self) -> None:
        """Modules under app.adapters are classified as ADAPTER."""
        assert classify_module("app.adapters") == Layer.ADAPTER
        assert classify_module("app.adapters.github") == Layer.ADAPTER
        assert classify_module("app.adapters.openrouter") == Layer.ADAPTER

    def test_presentation_layer_detection(self) -> None:
        """Modules under app.presentation are classified as PRESENTATION."""
        assert classify_module("app.presentation") == Layer.PRESENTATION
        assert classify_module("app.presentation.views") == Layer.PRESENTATION

    def test_external_module_returns_none(self) -> None:
        """External/third-party modules return None (not a layer)."""
        assert classify_module("fastapi") is None
        assert classify_module("pydantic") is None
        assert classify_module("os") is None
        assert classify_module("asyncio") is None
        assert classify_module("hypothesis") is None


# ---------------------------------------------------------------------------
# Requirement 23.1: Import adjacency enforcement
# ---------------------------------------------------------------------------


class TestImportAdjacency:
    """Tests for layer adjacency rules (Requirement 23.1).

    Every module import must target its own layer or exactly one adjacent layer.
    """

    def test_same_layer_allowed(self) -> None:
        """Imports within the same layer are always allowed."""
        allowed, reason = check_import_allowed(Layer.RUNTIME, "app.runtime.events.models")
        assert allowed is True
        assert reason == ""

    def test_application_can_import_runtime(self) -> None:
        """Application layer can import from Runtime (adjacent)."""
        allowed, reason = check_import_allowed(Layer.APPLICATION, "app.runtime.session")
        assert allowed is True

    def test_runtime_can_import_adapter(self) -> None:
        """Runtime layer can import from Adapter (adjacent via protocols)."""
        allowed, reason = check_import_allowed(Layer.RUNTIME, "app.adapters.github")
        assert allowed is True

    def test_presentation_can_import_application(self) -> None:
        """Presentation layer can import from Application (adjacent)."""
        allowed, reason = check_import_allowed(Layer.PRESENTATION, "app.api.endpoints")
        assert allowed is True

    def test_application_cannot_import_adapter(self) -> None:
        """Application layer cannot skip to Adapter (not adjacent)."""
        allowed, reason = check_import_allowed(Layer.APPLICATION, "app.adapters.github")
        assert allowed is False
        assert "crosses layer boundary" in reason
        assert "application → adapter" in reason

    def test_presentation_cannot_import_runtime(self) -> None:
        """Presentation layer cannot skip to Runtime (not adjacent)."""
        allowed, reason = check_import_allowed(Layer.PRESENTATION, "app.runtime.session")
        assert allowed is False
        assert "crosses layer boundary" in reason

    def test_presentation_cannot_import_adapter(self) -> None:
        """Presentation layer cannot skip two layers to Adapter."""
        allowed, reason = check_import_allowed(Layer.PRESENTATION, "app.adapters.github")
        assert allowed is False
        assert "crosses layer boundary" in reason

    def test_adapter_cannot_import_runtime(self) -> None:
        """Adapter layer cannot back-import to Runtime."""
        allowed, reason = check_import_allowed(Layer.ADAPTER, "app.runtime.session")
        assert allowed is False
        assert "crosses layer boundary" in reason

    def test_adapter_cannot_import_application(self) -> None:
        """Adapter layer cannot back-import to Application."""
        allowed, reason = check_import_allowed(Layer.ADAPTER, "app.api.endpoints")
        assert allowed is False
        assert "crosses layer boundary" in reason

    def test_external_imports_always_allowed(self) -> None:
        """External packages (stdlib, third-party) are always allowed."""
        for layer in Layer:
            allowed, _ = check_import_allowed(layer, "os")
            assert allowed is True
            allowed, _ = check_import_allowed(layer, "fastapi")
            assert allowed is True
            allowed, _ = check_import_allowed(layer, "pydantic")
            assert allowed is True


# ---------------------------------------------------------------------------
# Requirement 23.3: Application layer has no engineering logic
# ---------------------------------------------------------------------------


class TestApplicationLayerConstraints:
    """Tests that Application layer has no engineering logic (Requirement 23.3).

    Engineering logic means model-selection, tool-invocation, verification,
    or workflow-orchestration decisions.
    """

    def test_app_layer_imports_only_runtime(self) -> None:
        """Application layer can only import from Runtime, not Adapter."""
        # Valid: app.api -> app.runtime
        allowed, _ = check_import_allowed(Layer.APPLICATION, "app.runtime.inspector")
        assert allowed is True

        # Invalid: app.api -> app.adapters (skips runtime)
        allowed, reason = check_import_allowed(Layer.APPLICATION, "app.adapters.openrouter")
        assert allowed is False

    def test_actual_api_module_has_no_boundary_violations(self) -> None:
        """The real app.api.__init__ module respects boundary rules."""
        # Read actual source and check it
        import importlib.util

        spec = importlib.util.find_spec("app.api")
        if spec is None or spec.origin is None:
            pytest.skip("app.api module not importable in test environment")

        from pathlib import Path
        source = Path(spec.origin).read_text(encoding="utf-8")
        violations = check_module_boundaries("app.api", source)
        assert violations == [], f"API module has boundary violations: {violations}"


# ---------------------------------------------------------------------------
# Requirement 23.4: Runtime layer has no HTTP/transport logic
# ---------------------------------------------------------------------------


class TestRuntimeLayerConstraints:
    """Tests that Runtime layer has no HTTP/transport-specific logic (Requirement 23.4)."""

    def test_runtime_cannot_import_fastapi(self) -> None:
        """Runtime layer must not import FastAPI (HTTP-specific)."""
        allowed, reason = check_forbidden_imports(Layer.RUNTIME, "fastapi")
        assert allowed is False
        assert "forbidden" in reason.lower()

    def test_runtime_cannot_import_starlette(self) -> None:
        """Runtime layer must not import Starlette (HTTP-specific)."""
        allowed, reason = check_forbidden_imports(Layer.RUNTIME, "starlette")
        assert allowed is False
        assert "forbidden" in reason.lower()

    def test_runtime_cannot_import_starlette_submodule(self) -> None:
        """Runtime layer must not import Starlette submodules."""
        allowed, reason = check_forbidden_imports(Layer.RUNTIME, "starlette.responses")
        assert allowed is False

    def test_runtime_cannot_import_uvicorn(self) -> None:
        """Runtime layer must not import uvicorn (transport-specific)."""
        allowed, reason = check_forbidden_imports(Layer.RUNTIME, "uvicorn")
        assert allowed is False

    def test_runtime_can_import_asyncio(self) -> None:
        """Runtime layer can use asyncio (not transport-specific)."""
        allowed, _ = check_forbidden_imports(Layer.RUNTIME, "asyncio")
        assert allowed is True

    def test_runtime_can_import_pydantic(self) -> None:
        """Runtime layer can use pydantic (data validation, not transport)."""
        allowed, _ = check_forbidden_imports(Layer.RUNTIME, "pydantic")
        assert allowed is True


# ---------------------------------------------------------------------------
# Requirement 23.5: Adapter layer has no business/orchestration logic
# ---------------------------------------------------------------------------


class TestAdapterLayerConstraints:
    """Tests that Adapter layer cannot back-import to runtime/app (Requirement 23.5)."""

    def test_adapter_cannot_import_runtime_modules(self) -> None:
        """Adapter layer must not import runtime internals."""
        allowed, reason = check_import_allowed(Layer.ADAPTER, "app.runtime.policies")
        assert allowed is False

    def test_adapter_cannot_import_app_layer(self) -> None:
        """Adapter layer must not import application layer."""
        allowed, reason = check_import_allowed(Layer.ADAPTER, "app.api")
        assert allowed is False

    def test_adapter_can_import_own_modules(self) -> None:
        """Adapter layer can import other adapter modules."""
        allowed, _ = check_import_allowed(Layer.ADAPTER, "app.adapters.utils")
        assert allowed is True


# ---------------------------------------------------------------------------
# Requirement 23.6: Fail with error identifying offending import
# ---------------------------------------------------------------------------


class TestBoundaryViolationErrors:
    """Tests that violations produce clear errors (Requirement 23.6)."""

    def test_violation_identifies_source_module(self) -> None:
        """Violation error includes the source module path."""
        source = "from app.adapters.github import GithubConnector\n"
        violations = check_module_boundaries("app.api.endpoints", source)
        assert len(violations) == 1
        assert violations[0].source_module == "app.api.endpoints"

    def test_violation_identifies_source_layer(self) -> None:
        """Violation error includes the source layer."""
        source = "from app.adapters.github import GithubConnector\n"
        violations = check_module_boundaries("app.api.endpoints", source)
        assert violations[0].source_layer == Layer.APPLICATION

    def test_violation_identifies_imported_module(self) -> None:
        """Violation error includes the offending imported module."""
        source = "from app.adapters.github import GithubConnector\n"
        violations = check_module_boundaries("app.api.endpoints", source)
        assert violations[0].imported_module == "app.adapters.github"

    def test_violation_identifies_target_layer(self) -> None:
        """Violation error includes the target layer of the offending import."""
        source = "from app.adapters.github import GithubConnector\n"
        violations = check_module_boundaries("app.api.endpoints", source)
        assert violations[0].target_layer == Layer.ADAPTER

    def test_violation_str_is_descriptive(self) -> None:
        """String representation of violation is human-readable."""
        source = "from app.adapters.github import GithubConnector\n"
        violations = check_module_boundaries("app.api.endpoints", source)
        msg = str(violations[0])
        assert "app.api.endpoints" in msg
        assert "app.adapters.github" in msg
        assert "application" in msg

    def test_multiple_violations_all_reported(self) -> None:
        """Multiple violations in one module are all reported."""
        source = (
            "from app.adapters.github import GithubConnector\n"
            "from app.adapters.openrouter import OpenRouterClient\n"
        )
        violations = check_module_boundaries("app.api.endpoints", source)
        assert len(violations) == 2
        imported = {v.imported_module for v in violations}
        assert "app.adapters.github" in imported
        assert "app.adapters.openrouter" in imported

    def test_forbidden_import_in_runtime(self) -> None:
        """Runtime module importing FastAPI produces a violation."""
        source = "from fastapi import FastAPI\n"
        violations = check_module_boundaries("app.runtime.some_module", source)
        assert len(violations) == 1
        assert "forbidden" in violations[0].reason.lower()
        assert violations[0].imported_module == "fastapi"


# ---------------------------------------------------------------------------
# extract_imports_from_source tests
# ---------------------------------------------------------------------------


class TestExtractImports:
    """Tests for AST-based import extraction."""

    def test_import_statement(self) -> None:
        """Simple import statement is extracted."""
        source = "import os\n"
        assert "os" in extract_imports_from_source(source)

    def test_from_import_statement(self) -> None:
        """from ... import ... statement is extracted."""
        source = "from os.path import join\n"
        assert "os.path" in extract_imports_from_source(source)

    def test_multiple_imports(self) -> None:
        """Multiple imports are all extracted."""
        source = "import os\nimport sys\nfrom pathlib import Path\n"
        imports = extract_imports_from_source(source)
        assert "os" in imports
        assert "sys" in imports
        assert "pathlib" in imports

    def test_syntax_error_returns_empty(self) -> None:
        """Invalid Python source returns empty list (no crash)."""
        source = "this is not valid python {{{{"
        assert extract_imports_from_source(source) == []

    def test_nested_imports_extracted(self) -> None:
        """Imports inside functions are extracted."""
        source = "def foo():\n    import json\n"
        assert "json" in extract_imports_from_source(source)


# ---------------------------------------------------------------------------
# check_module_boundaries integration tests
# ---------------------------------------------------------------------------


class TestCheckModuleBoundaries:
    """Integration tests for the full boundary check on a module."""

    def test_valid_application_imports(self) -> None:
        """Application module with only valid imports passes."""
        source = (
            "from app.runtime.session import SessionManager\n"
            "from app.runtime.inspector import RuntimeInspector\n"
            "import asyncio\n"
            "from pydantic import BaseModel\n"
        )
        violations = check_module_boundaries("app.api.endpoints", source)
        assert violations == []

    def test_valid_runtime_imports(self) -> None:
        """Runtime module with only valid imports passes."""
        source = (
            "from app.runtime.events.models import Event\n"
            "from app.adapters import SomeAdapter\n"
            "import asyncio\n"
            "from dataclasses import dataclass\n"
        )
        violations = check_module_boundaries("app.runtime.workflow", source)
        assert violations == []

    def test_application_skipping_to_adapter_caught(self) -> None:
        """Application module importing adapter is caught."""
        source = "from app.adapters.github import GithubVCS\n"
        violations = check_module_boundaries("app.api.views", source)
        assert len(violations) == 1
        assert violations[0].target_layer == Layer.ADAPTER

    def test_runtime_importing_fastapi_caught(self) -> None:
        """Runtime module importing fastapi is caught."""
        source = (
            "from fastapi import HTTPException\n"
            "from app.runtime.events.models import Event\n"
        )
        violations = check_module_boundaries("app.runtime.session.manager", source)
        assert len(violations) == 1
        assert violations[0].imported_module == "fastapi"

    def test_external_module_not_checked(self) -> None:
        """Modules outside 'app' are not checked (returns empty)."""
        source = "from app.runtime.session import SessionManager\n"
        violations = check_module_boundaries("tests.test_session", source)
        assert violations == []

    def test_adapter_back_import_to_runtime_caught(self) -> None:
        """Adapter importing runtime internals is caught."""
        source = "from app.runtime.policies import PolicyEngine\n"
        violations = check_module_boundaries("app.adapters.github_impl", source)
        assert len(violations) == 1
        assert violations[0].source_layer == Layer.ADAPTER
        assert violations[0].target_layer == Layer.RUNTIME


# ---------------------------------------------------------------------------
# check_all_boundaries integration test (live codebase)
# ---------------------------------------------------------------------------


class TestLiveCodebaseBoundaries:
    """Run boundary checks against the actual codebase.

    Requirement 23.1: Verify that every module import targets either its own
    layer or exactly one adjacent layer.
    """

    def test_no_boundary_violations_in_codebase(self) -> None:
        """The actual app codebase has no layer boundary violations."""
        from pathlib import Path

        app_root = Path(__file__).parent.parent / "app"
        if not app_root.exists():
            pytest.skip("app directory not found")

        violations = check_all_boundaries(app_root)

        # Filter out any false positives from __pycache__
        real_violations = [
            v for v in violations if "__pycache__" not in v.source_module
        ]

        assert real_violations == [], (
            f"Found {len(real_violations)} boundary violation(s):\n"
            + "\n".join(str(v) for v in real_violations)
        )
