"""Layer Boundary Checker — enforces the 5-layer architecture.

Verifies that module imports target only their own layer or exactly one
adjacent layer. Detects violations of the layer separation contract:

- Presentation: only runtime API + event catalog, no orchestration logic
- Application: transport translation only, no engineering logic
- Runtime: protocol interfaces only, no HTTP/transport logic
- Adapter: one protocol call → one infrastructure call, no business logic

Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


# ---------------------------------------------------------------------------
# Layer definitions
# ---------------------------------------------------------------------------


class Layer(IntEnum):
    """The 5 layers of the Forge architecture, ordered from top to bottom."""

    PRESENTATION = 1
    APPLICATION = 2
    RUNTIME = 3
    ADAPTER = 4
    INFRASTRUCTURE = 5


# Mapping from module path prefixes to their layer.
# These are relative to the backend `app/` package.
LAYER_MODULES: dict[str, Layer] = {
    "app.api": Layer.APPLICATION,
    "app.runtime": Layer.RUNTIME,
    "app.adapters": Layer.ADAPTER,
    "app.db": Layer.INFRASTRUCTURE,
    "app.config": Layer.INFRASTRUCTURE,
}

# Adjacency: a layer may import from itself or one layer below.
# Presentation → Application, Application → Runtime, Runtime → Adapter, Adapter → Infrastructure.
ALLOWED_TARGETS: dict[Layer, set[Layer]] = {
    Layer.PRESENTATION: {Layer.PRESENTATION, Layer.APPLICATION},
    Layer.APPLICATION: {Layer.APPLICATION, Layer.RUNTIME},
    Layer.RUNTIME: {Layer.RUNTIME, Layer.ADAPTER},
    Layer.ADAPTER: {Layer.ADAPTER, Layer.INFRASTRUCTURE},
    Layer.INFRASTRUCTURE: {Layer.INFRASTRUCTURE},
}

# Forbidden import patterns for specific layers (content-level checks).
# These detect violations of the separation-of-concerns contract.

# Modules that indicate HTTP/transport logic (forbidden in Runtime layer)
TRANSPORT_MODULES = frozenset({
    "fastapi",
    "starlette",
    "uvicorn",
    "flask",
    "django",
    "aiohttp.web",
    "tornado.web",
})

# Modules that indicate engineering/orchestration logic (forbidden in Application layer)
ENGINEERING_MODULES = frozenset({
    "app.runtime.router",
    "app.runtime.verification",
    "app.runtime.policies",
    "app.runtime.dispatcher",
    "app.runtime.workspace",
    "app.runtime.planner",
    "app.runtime.commit",
    "app.runtime.finalization",
    "app.runtime.documentation",
})

# Modules that indicate business/orchestration logic (forbidden in Adapter layer)
BUSINESS_LOGIC_MODULES = frozenset({
    "app.runtime.router",
    "app.runtime.verification",
    "app.runtime.policies",
    "app.runtime.dispatcher",
    "app.runtime.planner",
    "app.runtime.workspace",
    "app.runtime.commit",
    "app.runtime.finalization",
    "app.runtime.documentation",
    "app.runtime.classifier",
    "app.runtime.clarification",
})


# ---------------------------------------------------------------------------
# Violation dataclass
# ---------------------------------------------------------------------------


@dataclass
class BoundaryViolation:
    """A detected layer boundary violation."""

    file_path: str
    import_path: str
    violation_reason: str


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def classify_layer(module_path: str) -> Layer | None:
    """Determine which layer a module path belongs to.

    Returns None if the module is external (not part of the app package).
    """
    for prefix, layer in LAYER_MODULES.items():
        if module_path == prefix or module_path.startswith(prefix + "."):
            return layer
    return None


def classify_file_layer(file_path: str) -> Layer | None:
    """Determine which layer a source file belongs to based on its path.

    Args:
        file_path: Relative or absolute path to a Python source file.

    Returns:
        The Layer the file belongs to, or None if it cannot be determined.
    """
    # Normalize path separators
    normalized = file_path.replace("\\", "/")

    if "/app/api/" in normalized or normalized.endswith("/app/api/__init__.py"):
        return Layer.APPLICATION
    if "/app/runtime/" in normalized or normalized.endswith("/app/runtime/__init__.py"):
        return Layer.RUNTIME
    if "/app/adapters/" in normalized or normalized.endswith("/app/adapters/__init__.py"):
        return Layer.ADAPTER
    if "/app/db/" in normalized or normalized.endswith("/app/db/__init__.py"):
        return Layer.INFRASTRUCTURE
    if "/app/config/" in normalized or normalized.endswith("/app/config/__init__.py"):
        return Layer.INFRASTRUCTURE

    return None


def extract_imports(source: str) -> list[str]:
    """Extract all import module paths from Python source code.

    Handles both `import X` and `from X import Y` forms.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def check_layer_adjacency(
    source_layer: Layer,
    target_layer: Layer,
    import_path: str,
    file_path: str,
) -> BoundaryViolation | None:
    """Check if an import from source_layer to target_layer is allowed.

    Returns a violation if the import crosses more than one layer boundary.
    """
    allowed = ALLOWED_TARGETS.get(source_layer, set())
    if target_layer not in allowed:
        return BoundaryViolation(
            file_path=file_path,
            import_path=import_path,
            violation_reason=(
                f"Import crosses layer boundary: {source_layer.name} "
                f"cannot import from {target_layer.name} "
                f"(only adjacent layers allowed)"
            ),
        )
    return None


def check_content_violations(
    source_layer: Layer,
    import_path: str,
    file_path: str,
) -> BoundaryViolation | None:
    """Check for content-level violations specific to each layer.

    Detects forbidden patterns like:
    - Runtime importing HTTP/transport modules
    - Application importing engineering logic directly
    - Adapter importing business logic
    """
    # Requirement 23.4: Runtime SHALL contain zero HTTP/transport logic
    if source_layer == Layer.RUNTIME:
        for transport_mod in TRANSPORT_MODULES:
            if import_path == transport_mod or import_path.startswith(transport_mod + "."):
                return BoundaryViolation(
                    file_path=file_path,
                    import_path=import_path,
                    violation_reason=(
                        f"Runtime layer cannot import transport/HTTP module '{import_path}'. "
                        f"Runtime depends only on protocol interfaces."
                    ),
                )

    # Requirement 23.3: Application SHALL contain zero engineering logic
    if source_layer == Layer.APPLICATION:
        for eng_mod in ENGINEERING_MODULES:
            if import_path == eng_mod or import_path.startswith(eng_mod + "."):
                return BoundaryViolation(
                    file_path=file_path,
                    import_path=import_path,
                    violation_reason=(
                        f"Application layer cannot import engineering logic module '{import_path}'. "
                        f"Application translates between transport and runtime API only."
                    ),
                )

    # Requirement 23.5: Adapter SHALL contain zero business/orchestration logic
    if source_layer == Layer.ADAPTER:
        for biz_mod in BUSINESS_LOGIC_MODULES:
            if import_path == biz_mod or import_path.startswith(biz_mod + "."):
                return BoundaryViolation(
                    file_path=file_path,
                    import_path=import_path,
                    violation_reason=(
                        f"Adapter layer cannot import business logic module '{import_path}'. "
                        f"Adapters translate one protocol call into one infrastructure call."
                    ),
                )

    return None


def check_file(file_path: str, source: str) -> list[BoundaryViolation]:
    """Check a single file for layer boundary violations.

    Args:
        file_path: Path to the Python source file.
        source: The source code content.

    Returns:
        List of violations found in the file.
    """
    source_layer = classify_file_layer(file_path)
    if source_layer is None:
        # File is not part of a recognized layer; skip.
        return []

    imports = extract_imports(source)
    violations: list[BoundaryViolation] = []

    for import_path in imports:
        # Determine target layer
        target_layer = classify_layer(import_path)

        if target_layer is not None:
            # Check adjacency (Requirement 23.1, 23.6)
            violation = check_layer_adjacency(
                source_layer, target_layer, import_path, file_path
            )
            if violation:
                violations.append(violation)

        # Check content-level violations regardless of whether target is
        # in a known layer (external modules can also violate, e.g. fastapi in runtime)
        content_violation = check_content_violations(
            source_layer, import_path, file_path
        )
        if content_violation:
            violations.append(content_violation)

    return violations


def check_boundaries(root_path: str | None = None) -> list[BoundaryViolation]:
    """Scan all Python files in the project for layer boundary violations.

    Args:
        root_path: Root directory of the project. Defaults to the backend
                   app directory (auto-detected relative to this module).

    Returns:
        List of all boundary violations found across the project.

    Raises:
        BoundaryCheckError: If the boundary check fails with violations.

    Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6
    """
    if root_path is None:
        # Default to the backend app directory
        root_path = str(
            Path(__file__).parent.parent.parent  # boundaries -> runtime -> app
        )

    violations: list[BoundaryViolation] = []

    for dirpath, _dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            if not filename.endswith(".py"):
                continue

            file_path = os.path.join(dirpath, filename)
            try:
                with open(file_path, encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            file_violations = check_file(file_path, source)
            violations.extend(file_violations)

    return violations


# ---------------------------------------------------------------------------
# Error type for programmatic boundary check failure
# ---------------------------------------------------------------------------


class BoundaryCheckError(Exception):
    """Raised when layer boundary violations are detected.

    Requirement 23.6: fail boundary check with error identifying
    the offending import path.
    """

    def __init__(self, violations: list[BoundaryViolation]) -> None:
        self.violations = violations
        paths = ", ".join(v.import_path for v in violations[:5])
        suffix = f" (and {len(violations) - 5} more)" if len(violations) > 5 else ""
        super().__init__(
            f"Layer boundary check failed with {len(violations)} violation(s): {paths}{suffix}"
        )


def enforce_boundaries(root_path: str | None = None) -> None:
    """Run boundary checks and raise BoundaryCheckError if violations exist.

    This is the entry point for CI/test integration.

    Args:
        root_path: Root directory of the project.

    Raises:
        BoundaryCheckError: If any boundary violations are found.

    Requirements: 23.6
    """
    violations = check_boundaries(root_path)
    if violations:
        raise BoundaryCheckError(violations)
