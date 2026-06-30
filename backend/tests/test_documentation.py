"""Unit tests for DocumentationMaintenance - Digital Twin diff and doc sync.

Tests cover:
- Twin diff computation (modules added, changed, removed) - Req 10.1
- DocWriter updates README and docs; emits doc_updated event - Req 10.2
- DocWriter failure: leave file unchanged, record drift, emit event - Req 10.3
- Post-finalization: update docs or record drift for each changed module - Req 10.4
- `document` build mode: diff all docs against twin, update mismatched - Req 10.5
- Skip files already matching twin in document mode - Req 10.6
"""

from __future__ import annotations

from typing import Any

import pytest

from app.runtime.documentation import (
    DocDriftEntry,
    DocFile,
    DocUpdateResult,
    DocumentationMaintenance,
    ModuleChange,
    TwinDiff,
    compute_twin_diff_from_twins,
)
from app.runtime.events.models import Event, EventType


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeDocWriter:
    def __init__(
        self,
        results: list[DocUpdateResult] | None = None,
        should_fail: bool = False,
        fail_error: str = "DocWriter unavailable",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results = results
        self.should_fail = should_fail
        self.fail_error = fail_error

    async def update_docs(self, diff: TwinDiff, doc_files: list[DocFile]) -> list[DocUpdateResult]:
        self.calls.append({"diff": diff, "doc_files": doc_files})
        if self.should_fail:
            raise RuntimeError(self.fail_error)
        if self.results is not None:
            return self.results
        return [DocUpdateResult(doc_file=f.path, success=True, new_content="updated") for f in doc_files]


class FakeTwinState:
    def __init__(self, doc_files: list[DocFile] | None = None, matching_files: set[str] | None = None) -> None:
        self._doc_files = doc_files or []
        self._matching_files = matching_files or set()

    def get_doc_files(self) -> list[DocFile]:
        return self._doc_files

    def is_doc_matching_twin(self, doc_file: DocFile) -> bool:
        return doc_file.path in self._matching_files


class FakeEventEmitter:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


# ---------------------------------------------------------------------------
# Tests: compute_twin_diff_from_twins utility (Req 10.1)
# ---------------------------------------------------------------------------


class TestComputeTwinDiffFromTwins:
    def test_no_changes(self) -> None:
        files = {"src/main.py", "src/utils.py"}
        diff = compute_twin_diff_from_twins(files, files)
        assert diff.is_empty
        assert "no changes" in diff.summary.lower()

    def test_files_added(self) -> None:
        diff = compute_twin_diff_from_twins({"src/main.py"}, {"src/main.py", "src/new.py"})
        assert diff.files_added == ["src/new.py"]
        assert not diff.is_empty

    def test_files_removed(self) -> None:
        diff = compute_twin_diff_from_twins({"src/main.py", "src/old.py"}, {"src/main.py"})
        assert diff.files_removed == ["src/old.py"]

    def test_files_modified(self) -> None:
        files = {"src/main.py", "src/utils.py"}
        diff = compute_twin_diff_from_twins(files, files, changed_files=["src/utils.py"])
        assert diff.files_modified == ["src/utils.py"]

    def test_combined(self) -> None:
        prior = {"src/main.py", "src/old.py", "src/utils.py"}
        current = {"src/main.py", "src/utils.py", "src/new.py"}
        diff = compute_twin_diff_from_twins(prior, current, changed_files=["src/utils.py"])
        assert diff.files_added == ["src/new.py"]
        assert diff.files_removed == ["src/old.py"]
        assert diff.files_modified == ["src/utils.py"]

    def test_summary(self) -> None:
        diff = compute_twin_diff_from_twins({"a.py"}, {"a.py", "b.py", "c.py"})
        assert "2 added" in diff.summary

    def test_commit_sha(self) -> None:
        diff = compute_twin_diff_from_twins(set(), {"new.py"}, commit_sha="abc123")
        assert diff.commit_sha == "abc123"
        assert diff.all_changes[0].details["commit_sha"] == "abc123"


# ---------------------------------------------------------------------------
# Tests: TwinDiff model
# ---------------------------------------------------------------------------


class TestTwinDiffModel:
    def test_empty(self) -> None:
        diff = TwinDiff()
        assert diff.is_empty
        assert not diff.has_changes
        assert "no changes" in diff.summary.lower()

    def test_with_changes(self) -> None:
        diff = TwinDiff(changes=[ModuleChange(path="a.py")])
        assert not diff.is_empty
        assert diff.has_changes

    def test_modules_constructor(self) -> None:
        diff = TwinDiff(
            modules_added=[ModuleChange("a.py", "added")],
            modules_changed=[ModuleChange("b.py", "changed")],
            modules_removed=[ModuleChange("c.py", "removed")],
        )
        assert len(diff.all_changes) == 3
        assert "1 added" in diff.summary


# ---------------------------------------------------------------------------
# Tests: DocumentationMaintenance.compute_twin_diff (Req 10.1)
# ---------------------------------------------------------------------------


class TestComputeTwinDiff:
    def test_computes_diff(self) -> None:
        m = DocumentationMaintenance(session_id="s1")
        diff = m.compute_twin_diff("abc123", ["src/main.py", "src/utils.py"])
        assert diff.commit_sha == "abc123"
        assert diff.session_id == "s1"
        assert len(diff.all_changes) == 2

    def test_empty(self) -> None:
        m = DocumentationMaintenance(session_id="s1")
        diff = m.compute_twin_diff("abc123", [])
        assert diff.is_empty

    def test_tracked(self) -> None:
        m = DocumentationMaintenance(session_id="s1")
        m.compute_twin_diff("sha1", ["a.py"])
        m.compute_twin_diff("sha2", ["b.py"])
        assert len(m.twin_diffs) == 2


# ---------------------------------------------------------------------------
# Tests: on_commit_done (Req 10.2, 10.3)
# ---------------------------------------------------------------------------


class TestOnCommitDone:
    @pytest.fixture
    def twin_state(self) -> FakeTwinState:
        return FakeTwinState(doc_files=[DocFile(path="README.md"), DocFile(path="docs/API.md")])

    @pytest.fixture
    def emitter(self) -> FakeEventEmitter:
        return FakeEventEmitter()

    @pytest.mark.asyncio
    async def test_invokes_writer(self, twin_state: FakeTwinState, emitter: FakeEventEmitter) -> None:
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=twin_state, event_emitter=emitter, session_id="s1")
        diff, drift = await m.on_commit_done("abc123", ["src/module.py"])
        assert len(writer.calls) == 1
        assert writer.calls[0]["diff"].commit_sha == "abc123"

    @pytest.mark.asyncio
    async def test_emits_doc_updated(self, twin_state: FakeTwinState, emitter: FakeEventEmitter) -> None:
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=twin_state, event_emitter=emitter, session_id="s1")
        await m.on_commit_done("abc123", ["src/module.py"])
        events = emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(events) == 1
        assert events[0].payload["commit_sha"] == "abc123"
        assert "twin_diff_summary" in events[0].payload

    @pytest.mark.asyncio
    async def test_no_call_on_empty(self, twin_state: FakeTwinState, emitter: FakeEventEmitter) -> None:
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=twin_state, event_emitter=emitter, session_id="s1")
        await m.on_commit_done("abc123", [])
        assert len(writer.calls) == 0

    @pytest.mark.asyncio
    async def test_drift_on_failure(self, twin_state: FakeTwinState, emitter: FakeEventEmitter) -> None:
        writer = FakeDocWriter(should_fail=True, fail_error="AI down")
        m = DocumentationMaintenance(doc_writer=writer, twin_state=twin_state, event_emitter=emitter, session_id="s1")
        diff, drift = await m.on_commit_done("abc123", ["src/module.py"])
        assert len(drift) > 0
        assert any("doc_writer_error" in e.reason for e in drift)
        drift_events = emitter.events_of_type(EventType.DOC_DRIFT)
        assert len(drift_events) == 1

    @pytest.mark.asyncio
    async def test_drift_no_writer(self, twin_state: FakeTwinState, emitter: FakeEventEmitter) -> None:
        m = DocumentationMaintenance(twin_state=twin_state, event_emitter=emitter, session_id="s1")
        diff, drift = await m.on_commit_done("abc123", ["src/module.py"])
        assert len(drift) > 0
        assert any("no_doc_writer_available" in e.reason for e in drift)

    @pytest.mark.asyncio
    async def test_updated_files_tracked(self, twin_state: FakeTwinState, emitter: FakeEventEmitter) -> None:
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=twin_state, event_emitter=emitter, session_id="s1")
        await m.on_commit_done("abc123", ["src/module.py"])
        assert len(m.updated_files) > 0

    @pytest.mark.asyncio
    async def test_result_supports_attribute_access(self, twin_state: FakeTwinState, emitter: FakeEventEmitter) -> None:
        """CommitDoneResult supports both unpacking and attribute access."""
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=twin_state, event_emitter=emitter, session_id="s1")
        result = await m.on_commit_done("abc123", ["src/module.py"])
        assert result.commit_sha == "abc123"
        assert len(result.all_changes) == 1


# ---------------------------------------------------------------------------
# Tests: on_finalization (Req 10.4)
# ---------------------------------------------------------------------------


class TestOnFinalization:
    @pytest.fixture
    def emitter(self) -> FakeEventEmitter:
        return FakeEventEmitter()

    @pytest.mark.asyncio
    async def test_drift_for_uncovered(self, emitter: FakeEventEmitter) -> None:
        m = DocumentationMaintenance(event_emitter=emitter, session_id="s1")
        m.compute_twin_diff("sha1", ["src/new.py", "src/main.py"])
        m._updated_files.append("src/main.py")
        drift = await m.finalize_documentation()
        assert len(drift) == 1
        assert drift[0].module_path == "src/new.py"

    @pytest.mark.asyncio
    async def test_no_drift_when_all_updated(self, emitter: FakeEventEmitter) -> None:
        m = DocumentationMaintenance(event_emitter=emitter, session_id="s1")
        m.compute_twin_diff("sha1", ["src/main.py"])
        m._updated_files.append("src/main.py")
        drift = await m.finalize_documentation()
        assert drift == []

    @pytest.mark.asyncio
    async def test_emits_drift_event(self, emitter: FakeEventEmitter) -> None:
        m = DocumentationMaintenance(event_emitter=emitter, session_id="s1")
        m.compute_twin_diff("sha1", ["src/new.py"])
        await m.finalize_documentation()
        drift_events = emitter.events_of_type(EventType.DOC_DRIFT)
        assert len(drift_events) == 1
        assert drift_events[0].payload["total_drift_count"] >= 1


# ---------------------------------------------------------------------------
# Tests: run_document_mode (Req 10.5, 10.6)
# ---------------------------------------------------------------------------


class TestDocumentMode:
    @pytest.fixture
    def emitter(self) -> FakeEventEmitter:
        return FakeEventEmitter()

    @pytest.mark.asyncio
    async def test_updates_mismatched(self, emitter: FakeEventEmitter) -> None:
        doc_files = [DocFile(path="docs/api.md"), DocFile(path="docs/guide.md")]
        ts = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=ts, event_emitter=emitter, session_id="s1")
        results = await m.run_document_mode()
        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_skips_matching(self, emitter: FakeEventEmitter) -> None:
        doc_files = [DocFile(path="docs/api.md"), DocFile(path="docs/guide.md")]
        ts = FakeTwinState(doc_files=doc_files, matching_files={"docs/api.md"})
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=ts, event_emitter=emitter, session_id="s1")
        results = await m.run_document_mode()
        assert len(results) == 1
        assert results[0].doc_file == "docs/guide.md"

    @pytest.mark.asyncio
    async def test_all_match_returns_empty(self, emitter: FakeEventEmitter) -> None:
        doc_files = [DocFile(path="docs/api.md")]
        ts = FakeTwinState(doc_files=doc_files, matching_files={"docs/api.md"})
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=ts, event_emitter=emitter, session_id="s1")
        results = await m.run_document_mode()
        assert results == []
        assert len(writer.calls) == 0

    @pytest.mark.asyncio
    async def test_drift_on_failure(self, emitter: FakeEventEmitter) -> None:
        doc_files = [DocFile(path="docs/api.md")]
        ts = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter(should_fail=True, fail_error="Service down")
        m = DocumentationMaintenance(doc_writer=writer, twin_state=ts, event_emitter=emitter, session_id="s1")
        results = await m.run_document_mode()
        assert results == []
        assert len(m.drift_entries) == 1

    @pytest.mark.asyncio
    async def test_emits_doc_updated(self, emitter: FakeEventEmitter) -> None:
        doc_files = [DocFile(path="docs/api.md")]
        ts = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter()
        m = DocumentationMaintenance(doc_writer=writer, twin_state=ts, event_emitter=emitter, session_id="s1")
        await m.run_document_mode()
        events = emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(events) == 1
        assert "docs/api.md" in events[0].payload["files"]
