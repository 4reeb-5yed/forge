"""Core data models and enums for the Forge Runtime.

Defines the fundamental domain types used across the runtime: capabilities,
roles, tasks, workflow state, session context, and the digital twin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypedDict


class BuildMode(str, Enum):
    """The kind of build requested for a session.

    new: Build new functionality from scratch.
    extend: Extend existing functionality.
    analyze: Analyze the repository without making changes.
    document: Update documentation to match current code state.
    """

    NEW = "new"
    EXTEND = "extend"
    ANALYZE = "analyze"
    DOCUMENT = "document"


class OperationalMode(str, Enum):
    """The runtime's operational mode.

    OPERATIONAL: All minimum-required capabilities are met; builds are allowed.
    DEGRADED: One or more required capabilities are missing; builds are refused.
    """

    OPERATIONAL = "operational"
    DEGRADED = "degraded"


class Capability(str, Enum):
    """Named, discoverable capabilities in the Forge Runtime.

    Each member represents a specific concrete resource that can be registered
    in the Capability Registry and resolved by name.
    """

    AI_CLARIFICATION = "ai_clarification"
    AI_ARCHITECT = "ai_architect"
    AI_PLANNER = "ai_planner"
    AI_CODER = "ai_coder"
    AI_REVIEWER = "ai_reviewer"
    AI_DOC_WRITER = "ai_doc_writer"
    AI_INTERRUPT_HANDLER = "ai_interrupt_handler"
    TOOL_AIDER = "tool_aider"
    TOOL_OPENHANDS = "tool_openhands"
    VCS_GITHUB = "vcs_github"
    STORE_R2 = "store_r2"
    STORE_LOCAL = "store_local"
    DB_POSTGRES = "db_postgres"
    VECTOR_CHROMA = "vector_chroma"
    RUNTIME_DOCKER = "runtime_docker"


class CapabilityKind(str, Enum):
    """Categories of capabilities for kind-based lookups.

    Used by the Registry to answer questions like 'is any VCS connector available?'
    without knowing the specific capability name.
    """

    AI_PROVIDER = "ai_provider"
    CODING_TOOL = "coding_tool"
    VCS_CONNECTOR = "vcs_connector"
    ARTIFACT_STORE = "artifact_store"
    VECTOR_STORE = "vector_store"
    VERIFIER = "verifier"
    CONTEXT_PROVIDER = "context_provider"
    DOC_WRITER = "doc_writer"


class Role(str, Enum):
    """Abstract AI function roles resolved to concrete providers by the Model Router.

    Each role represents a distinct reasoning responsibility in the build workflow.
    The Model Router maps each role to an ordered fallback chain of providers.
    """

    CLARIFICATION = "clarification"
    ARCHITECT = "architect"
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    DOC_WRITER = "doc_writer"
    INTERRUPT_HANDLER = "interrupt_handler"


@dataclass(frozen=True)
class CapabilityEntry:
    """A registered capability in the Capability Registry.

    Records which capability is available, its kind, health status,
    and any associated metadata for resolution.
    """

    name: Capability
    kind: CapabilityKind
    healthy: bool = True
    roles: list[Role] = field(default_factory=list)
    provider_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilitySummary:
    """Snapshot of available, degraded, and missing-required capabilities.

    Used to determine operational mode and surfaced to the user via the Inspector.
    """

    available: dict[Capability, str] = field(default_factory=dict)  # capability -> status
    degraded: list[Capability] = field(default_factory=list)
    missing_required: list[Capability] = field(default_factory=list)
    missing_reasons: dict[str, str] = field(default_factory=dict)  # capability/kind -> reason
    soft_degradations: list[str] = field(default_factory=list)  # soft capability descriptions
    mode: str = "degraded"  # "operational" or "degraded"
    can_operate: bool = False


@dataclass
class Task:
    """A single unit of work in a build plan.

    Tasks form a dependency graph and are dispatched to workers/workspaces
    for execution, verification, and commit.
    """

    id: str
    title: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    status: Literal[
        "pending", "running", "verifying", "committed", "failed", "skipped"
    ] = "pending"
    attempts: int = 0
    assigned_tool: Capability | None = None
    workspace_id: str | None = None


class ForgeState(TypedDict, total=False):
    """LangGraph workflow state object.

    Per design R4, large/growing objects (twin, audit log) are referenced
    by handle, not embedded. This keeps checkpoints small and cheap.
    """

    session_id: str
    status: str
    build_mode: Literal["new", "extend", "analyze", "document"]
    tasks: list[Task]
    current_task_id: str | None
    digital_twin: str  # reference/handle into twin store
    session_context: str  # reference/handle into session context store
    verification_results: dict[str, Any]
    decisions: list[str]  # decision_ids referencing the audit trail
    errors: list[dict[str, Any]]


@dataclass
class SessionContext:
    """Persistent per-session working memory injected into prompt-constructing nodes.

    Updated by the clarify node and any node that records a new decision/assumption.
    The render is deterministic so prompts are reproducible.
    """

    session_id: str = ""
    goals: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)

    def render_for_prompt(self) -> str:
        """Deterministic, ordered serialization for prompt construction.

        Produces identical output for identical context, ensuring reproducibility.
        """
        sections: list[str] = []
        if self.goals:
            sections.append("Goals:\n" + "\n".join(f"- {g}" for g in self.goals))
        if self.constraints:
            sections.append(
                "Constraints:\n" + "\n".join(f"- {c}" for c in self.constraints)
            )
        if self.assumptions:
            sections.append(
                "Assumptions:\n" + "\n".join(f"- {a}" for a in self.assumptions)
            )
        if self.decisions:
            sections.append(
                "Decisions:\n" + "\n".join(f"- {d}" for d in self.decisions)
            )
        if self.preferences:
            sections.append(
                "Preferences:\n" + "\n".join(f"- {p}" for p in self.preferences)
            )
        return "\n\n".join(sections)


@dataclass(frozen=True)
class DocFileEntry:
    """A single documentation file tracked by the Digital Twin."""

    path: str
    code_references: list[str] = field(default_factory=list)
    last_synced_sha: str = ""
    drift_detected: bool = False


@dataclass
class DocumentationState:
    """First-class model of documentation in the Digital Twin (design R8).

    Tracks documentation files, their code references, sync status, and drift.
    Documentation is a verifiable artifact, not a side effect.
    """

    doc_files: list[DocFileEntry] = field(default_factory=list)
    readme_present: bool = False
    coverage: dict[str, bool] = field(default_factory=dict)  # module -> documented?


@dataclass(frozen=True)
class FileEntry:
    """A file in the Digital Twin's file index."""

    path: str
    role: Literal["entry", "test", "config", "source", "doc"] = "source"
    size: int = 0
    hash: str = ""


@dataclass
class DigitalTwin:
    """Lean structured model of the repository (v1).

    Contains language/framework detection, a file index with roles,
    git summary, entry points, and documentation state. Dependency graph
    and API-surface extraction are deferred behind the same interface.
    """

    language: str | None = None
    framework: str | None = None
    file_index: list[FileEntry] = field(default_factory=list)
    git_summary: str = ""
    entry_points: list[str] = field(default_factory=list)
    documentation_state: DocumentationState = field(default_factory=DocumentationState)
