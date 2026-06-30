"""Plugin protocol interfaces for the Forge Runtime.

Every capability in the system implements one of these protocols. The runtime core
depends only on these interfaces (dependency inversion), never on concrete adapters.
Adding a capability = create one file implementing the protocol + register one name.

All protocols are @runtime_checkable so the Capability Registry can verify structural
conformance at registration time.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from app.runtime.types import DocUpdateResult, Health, ToolResult, TwinDiff, VerifyResult


@runtime_checkable
class AIProvider(Protocol):
    """AI model provider — completes prompts and streams tokens.

    Used by the Model Router to resolve Role-based requests to concrete model calls.
    """

    name: str

    async def complete(self, messages: list[dict[str, Any]], model: str, **kwargs: Any) -> str:
        """Complete a prompt and return the full response text."""
        ...

    async def stream(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> AsyncIterator[str]:
        """Stream tokens from a prompt completion."""
        ...

    async def health_check(self) -> Health:
        """Check provider connectivity and readiness."""
        ...


@runtime_checkable
class CodingTool(Protocol):
    """Tool that writes/edits code (e.g. Aider, OpenHands).

    Executes a coding task in an isolated workspace, returning the result.
    """

    name: str

    async def execute(
        self, task: Any, workspace: Any, context: Any
    ) -> ToolResult:
        """Execute a coding task in the given workspace with session context."""
        ...

    async def health_check(self) -> Health:
        """Check tool availability and readiness."""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Store for build artifacts (e.g. Cloudflare R2, local filesystem).

    Provides key-value storage for specification artifacts, build outputs, etc.
    """

    async def save(self, key: str, data: bytes) -> None:
        """Save artifact data under the given key."""
        ...

    async def load(self, key: str) -> bytes:
        """Load artifact data by key. Raises if key not found."""
        ...

    async def list(self, prefix: str) -> list[str]:
        """List artifact keys matching the given prefix."""
        ...

    async def delete(self, key: str) -> None:
        """Delete an artifact by key."""
        ...

    async def health_check(self) -> Health:
        """Check store connectivity and readiness."""
        ...


@runtime_checkable
class VCSConnector(Protocol):
    """Version control connector (e.g. GitHub).

    Handles repository operations: clone, read, commit, push, log.
    """

    async def clone(self, url: str, token: str, dest: str) -> None:
        """Clone a repository to the destination path."""
        ...

    async def read_file(self, path: str) -> str:
        """Read a file from the working repository."""
        ...

    async def list_files(self, repo_path: str) -> list[str]:
        """List all tracked files in the repository."""
        ...

    async def commit(self, repo_path: str, message: str, files: list[str]) -> str:
        """Commit specified files and return the commit SHA."""
        ...

    async def push(self, repo_path: str) -> None:
        """Push commits to the remote."""
        ...

    async def get_log(self, repo_path: str, n: int) -> list[dict[str, Any]]:
        """Get the last n commit entries from the repository log."""
        ...

    async def health_check(self) -> Health:
        """Check VCS connectivity and readiness."""
        ...


@runtime_checkable
class VectorStore(Protocol):
    """Semantic retrieval store (e.g. ChromaDB).

    Indexes code/document chunks and retrieves them by semantic similarity.
    """

    async def index(self, session_id: str, chunks: list[Any]) -> None:
        """Index a list of chunks for the given session."""
        ...

    async def query(self, session_id: str, text: str, k: int) -> list[Any]:
        """Query for the top-k semantically similar chunks."""
        ...

    async def health_check(self) -> Health:
        """Check vector store connectivity and readiness."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """Checks task output (lint, tests, LLM review).

    Each verifier declares whether it is blocking (gate) or advisory.
    """

    name: str
    blocking: bool

    async def verify(self, workspace: Any, task: Any) -> VerifyResult:
        """Verify task output in the given workspace."""
        ...

    async def health_check(self) -> Health:
        """Check verifier readiness."""
        ...


@runtime_checkable
class ContextProvider(Protocol):
    """Provides context for prompts from the twin, vector store, or other sources.

    Gathers relevant code/document chunks to enrich prompt construction.
    """

    async def gather(self, session_id: str, task: Any) -> list[Any]:
        """Gather context chunks relevant to the given task."""
        ...

    async def health_check(self) -> Health:
        """Check context provider readiness."""
        ...


@runtime_checkable
class DocWriter(Protocol):
    """Updates documentation files from a computed twin diff.

    Documentation is a first-class verifiable artifact, not a side effect.
    """

    name: str

    async def update_docs(
        self, workspace: Any, diff: TwinDiff, context: Any
    ) -> DocUpdateResult:
        """Update documentation in the workspace based on the twin diff."""
        ...

    async def health_check(self) -> Health:
        """Check doc writer readiness."""
        ...
