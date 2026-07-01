"""Layer boundary enforcement for the Forge Runtime.

Verifies that module imports target only their own layer or exactly one adjacent
layer. Fails with an error identifying the offending import path when a violation
is detected.

Layer adjacency rules (strict one-hop):
  Presentation → Application (only)
  Application → Runtime (only)
  Runtime → Adapters (only; via protocol interfaces)
  Adapters → (external packages only; no back-imports to runtime/application)

Within a layer, imports are unrestricted.

Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class Layer(str, Enum):
    """Architecture layers in the Forge system."""

    PRESENTATION = "presentation"
    APPLICATION = "application"
    RUNTIME = "runtime"
    ADAPTER = "adapter"
    SHARED = "shared"  # Shared types used by multiple layers


# Layer adjacency: which layers can a given layer import from?
# Each layer may import from itself and from exactly one adjacent layer.
# Adapters and Runtime can both import from the shared layer.
LAYER_ADJACENCY: dict[Layer, set[Layer]] = {
    Layer.PRESENTATION: {Layer.PRESENTATION, Layer.APPLICATION},
    Layer.APPLICATION: {Layer.APPLICATION, Layer.RUNTIME},
    Layer.RUNTIME: {Layer.RUNTIME, Layer.ADAPTER, Layer.SHARED},
    Layer.ADAPTER: {Layer.ADAPTER, Layer.SHARED},
    Layer.SHARED: {Layer.SHARED},
}

# Module path prefixes that identify each layer
LAYER_PREFIXES: dict[str, Layer] = {
    "app.api": Layer.APPLICATION,
    "app.runtime": Layer.RUNTIME,
    "app.adapters": Layer.ADAPTER,
    "app.presentation": Layer.PRESENTATION,
    "app.shared": Layer.SHARED,
}

# Forbidden content patterns per layer (Req 23.2-23.5)
# These define what a layer must NOT contain
FORBIDDEN_IMPORTS: dict[Layer, set[str]] = {
    # Application layer: no engineering logic (model-selection, tool-invocation,
    # verification, workflow-orchestration)
    Layer.APPLICATION: set(),
    # Runtime layer: no HTTP/transport-specific logic
    Layer.RUNTIME: {"fastapi", "starlette", "uvicorn", "flask", "django"},
    # Adapter layer: no business logic, no orchestration
    Layer.ADAPTER: set(),
}


@dataclass(frozen=True)
class BoundaryViolation:
    """A detected layer boundary violation."""

    source_module: str
    source_layer: Layer
    imported_module: str
    target_layer: Layer | None
    reason: str

    def __str__(self) -> str:
        return (
            f"Boundary violation in '{self.source_module}' ({self.source_layer.value}): "
            f"imports '{self.imported_module}' — {self.reason}"
        )


def classify_module(module_path: str) -> Layer | None:
    """Determine which layer a module belongs to based on its import path.

    Returns None if the module is external (not part of the app package).
    """
    for prefix, layer in sorted(LAYER_PREFIXES.items(), key=lambda x: -len(x[0])):
        if module_path.startswith(prefix):
            return layer
    return None


def check_import_allowed(source_layer: Layer, target_module: str) -> tuple[bool, str]:
    """Check if a source layer is allowed to import the target module.

    Returns (allowed, reason) where reason explains any violation.
    """
    target_layer = classify_module(target_module)

    # External imports are always allowed (e.g., stdlib, third-party)
    if target_layer is None:
        return True, ""

    # Same layer is always allowed
    if target_layer == source_layer:
        return True, ""

    # Check adjacency
    if target_layer not in LAYER_ADJACENCY[source_layer]:
        return False, (
            f"crosses layer boundary: {source_layer.value} → {target_layer.value} "
            f"is not an adjacent layer (allowed: "
            f"{', '.join(l.value for l in LAYER_ADJACENCY[source_layer] if l != source_layer)})"
        )

    return True, ""


def check_forbidden_imports(source_layer: Layer, imported_module: str) -> tuple[bool, str]:
    """Check if a layer imports something it should not contain.

    For example, the Runtime layer must not import HTTP/transport modules.
    """
    forbidden = FORBIDDEN_IMPORTS.get(source_layer, set())
    for forbidden_prefix in forbidden:
        if imported_module == forbidden_prefix or imported_module.startswith(f"{forbidden_prefix}."):
            return False, (
                f"layer '{source_layer.value}' must not import '{imported_module}' "
                f"(forbidden transport/HTTP-specific dependency)"
            )
    return True, ""


def extract_imports_from_source(source: str) -> list[str]:
    """Extract all import module paths from Python source code."""
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


def check_module_boundaries(
    module_path: str, source: str
) -> list[BoundaryViolation]:
    """Check a single module's imports for layer boundary violations.

    Args:
        module_path: The dotted module path (e.g., 'app.api.endpoints')
        source: The Python source code of the module

    Returns:
        A list of BoundaryViolation instances (empty means no violations).
    """
    source_layer = classify_module(module_path)
    if source_layer is None:
        # Not part of the app package — skip
        return []

    imports = extract_imports_from_source(source)
    violations: list[BoundaryViolation] = []

    for imported in imports:
        # Check layer adjacency
        allowed, reason = check_import_allowed(source_layer, imported)
        if not allowed:
            target_layer = classify_module(imported)
            violations.append(
                BoundaryViolation(
                    source_module=module_path,
                    source_layer=source_layer,
                    imported_module=imported,
                    target_layer=target_layer,
                    reason=reason,
                )
            )
            continue

        # Check forbidden imports for the layer
        allowed, reason = check_forbidden_imports(source_layer, imported)
        if not allowed:
            violations.append(
                BoundaryViolation(
                    source_module=module_path,
                    source_layer=source_layer,
                    imported_module=imported,
                    target_layer=None,
                    reason=reason,
                )
            )

    return violations


def check_all_boundaries(app_root: str | Path) -> list[BoundaryViolation]:
    """Walk the app directory and check all Python modules for boundary violations.

    Args:
        app_root: Path to the 'app' directory

    Returns:
        A list of all BoundaryViolation instances found.
    """
    app_root = Path(app_root)
    violations: list[BoundaryViolation] = []

    for py_file in app_root.rglob("*.py"):
        # Convert file path to module path
        rel_path = py_file.relative_to(app_root.parent)
        module_path = str(rel_path).replace(os.sep, ".").replace("/", ".")
        if module_path.endswith(".py"):
            module_path = module_path[:-3]
        if module_path.endswith(".__init__"):
            module_path = module_path[:-9]

        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        violations.extend(check_module_boundaries(module_path, source))

    return violations
