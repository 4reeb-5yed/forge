"""Tests for plugin protocol interfaces and shared types."""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from app.runtime.protocols import (
    AIProvider,
    ArtifactStore,
    CodingTool,
    ContextProvider,
    DocWriter,
    VCSConnector,
    VectorStore,
    Verifier,
)
from app.runtime.types import (
    DocUpdateResult,
    Health,
    HealthStatus,
    ToolResult,
    TwinDiff,
    VerifyContext,
    VerifyResult,
    VerifyStatus,
)


# --- Health type tests ---


class TestHealth:
    def test_healthy_factory(self):
        h = Health.healthy(latency_ms=5.0)
        assert h.ok is True
        assert h.status == HealthStatus.OK
        assert h.latency_ms == 5.0
        assert h.message == ""

    def test_unhealthy_factory(self):
        h = Health.unhealthy("timeout", latency_ms=1000.0)
        assert h.ok is False
        assert h.status == HealthStatus.UNHEALTHY
        assert h.message == "timeout"
        assert h.latency_ms == 1000.0

    def test_degraded_factory(self):
        h = Health.degraded("high latency")
        assert h.ok is False
        assert h.status == HealthStatus.DEGRADED
        assert h.message == "high latency"

    def test_frozen(self):
        h = Health.healthy()
        with pytest.raises(Exception):
            h.ok = False  # type: ignore[misc]


# --- Shared types tests ---


class TestToolResult:
    def test_success_result(self):
        r = ToolResult(success=True, files_modified=["a.py"], output="done")
        assert r.success is True
        assert r.files_modified == ["a.py"]

    def test_defaults(self):
        r = ToolResult(success=False)
        assert r.files_modified == []
        assert r.output == ""
        assert r.error == ""

    def test_frozen(self):
        r = ToolResult(success=True)
        with pytest.raises(Exception):
            r.success = False  # type: ignore[misc]


class TestVerifyResult:
    def test_construction(self):
        vr = VerifyResult(
            stage_name="tests", status=VerifyStatus.PASSED, blocking=True, duration_ms=250.0
        )
        assert vr.stage_name == "tests"
        assert vr.status == VerifyStatus.PASSED
        assert vr.blocking is True
        assert vr.duration_ms == 250.0

    def test_failed_status(self):
        vr = VerifyResult(
            stage_name="lint", status=VerifyStatus.FAILED, blocking=False, detail="3 errors"
        )
        assert vr.status == VerifyStatus.FAILED
        assert vr.detail == "3 errors"


class TestVerifyContext:
    def test_construction(self):
        vc = VerifyContext(
            session_id="sess-1",
            task_id="task-1",
            workspace_path="/tmp/ws",
            files_modified=["main.py"],
            base_ref="abc123",
        )
        assert vc.session_id == "sess-1"
        assert vc.workspace_path == "/tmp/ws"
        assert vc.files_modified == ["main.py"]


class TestDocUpdateResult:
    def test_success(self):
        r = DocUpdateResult(success=True, files_updated=["README.md"])
        assert r.success is True
        assert r.files_updated == ["README.md"]
        assert r.files_created == []


class TestTwinDiff:
    def test_construction(self):
        td = TwinDiff(files_added=["new.py"], files_removed=["old.py"], summary="refactored")
        assert td.files_added == ["new.py"]
        assert td.files_removed == ["old.py"]
        assert td.summary == "refactored"


# --- Protocol runtime_checkable tests ---


class FakeAIProvider:
    name = "fake"

    async def complete(self, messages: list[dict[str, Any]], model: str, **kwargs: Any) -> str:
        return "response"

    async def stream(
        self, messages: list[dict[str, Any]], model: str, **kwargs: Any
    ) -> AsyncIterator[str]:
        yield "token"

    async def health_check(self) -> Health:
        return Health.healthy()


class FakeCodingTool:
    name = "fake-tool"

    async def execute(self, task: Any, workspace: Any, context: Any) -> ToolResult:
        return ToolResult(success=True)

    async def health_check(self) -> Health:
        return Health.healthy()


class FakeArtifactStore:
    async def save(self, key: str, data: bytes) -> None:
        pass

    async def load(self, key: str) -> bytes:
        return b""

    async def list(self, prefix: str) -> list[str]:
        return []

    async def delete(self, key: str) -> None:
        pass

    async def health_check(self) -> Health:
        return Health.healthy()


class FakeVCSConnector:
    async def clone(self, url: str, token: str, dest: str) -> None:
        pass

    async def read_file(self, path: str) -> str:
        return ""

    async def list_files(self, repo_path: str) -> list[str]:
        return []

    async def commit(self, repo_path: str, message: str, files: list[str]) -> str:
        return "abc123"

    async def push(self, repo_path: str) -> None:
        pass

    async def get_log(self, repo_path: str, n: int) -> list[dict[str, Any]]:
        return []

    async def health_check(self) -> Health:
        return Health.healthy()


class FakeVectorStore:
    async def index(self, session_id: str, chunks: list[Any]) -> None:
        pass

    async def query(self, session_id: str, text: str, k: int) -> list[Any]:
        return []

    async def health_check(self) -> Health:
        return Health.healthy()


class FakeVerifier:
    name = "fake-verifier"
    blocking = True

    async def verify(self, workspace: Any, task: Any) -> VerifyResult:
        return VerifyResult(stage_name="fake", status=VerifyStatus.PASSED, blocking=True)

    async def health_check(self) -> Health:
        return Health.healthy()


class FakeContextProvider:
    async def gather(self, session_id: str, task: Any) -> list[Any]:
        return []

    async def health_check(self) -> Health:
        return Health.healthy()


class FakeDocWriter:
    name = "fake-doc-writer"

    async def update_docs(self, workspace: Any, diff: TwinDiff, context: Any) -> DocUpdateResult:
        return DocUpdateResult(success=True)

    async def health_check(self) -> Health:
        return Health.healthy()


class TestProtocolRuntimeCheckable:
    """Verify that structural subtyping works via runtime_checkable."""

    def test_ai_provider(self):
        assert isinstance(FakeAIProvider(), AIProvider)

    def test_coding_tool(self):
        assert isinstance(FakeCodingTool(), CodingTool)

    def test_artifact_store(self):
        assert isinstance(FakeArtifactStore(), ArtifactStore)

    def test_vcs_connector(self):
        assert isinstance(FakeVCSConnector(), VCSConnector)

    def test_vector_store(self):
        assert isinstance(FakeVectorStore(), VectorStore)

    def test_verifier(self):
        assert isinstance(FakeVerifier(), Verifier)

    def test_context_provider(self):
        assert isinstance(FakeContextProvider(), ContextProvider)

    def test_doc_writer(self):
        assert isinstance(FakeDocWriter(), DocWriter)

    def test_non_conforming_class_fails(self):
        """A class missing required methods should not match the protocol."""

        class NotAProvider:
            pass

        assert not isinstance(NotAProvider(), AIProvider)
        assert not isinstance(NotAProvider(), CodingTool)
        assert not isinstance(NotAProvider(), Verifier)
