"""Unit tests for DocumentationMaintenance — Digital Twin diff and doc sync.

Tests cover:
- Twin diff computation (modules added, changed, removed) — Req 10.1
- DocWriter updates README and docs; emits doc_updated event — Req 10.2
- DocWriter failure: leave file unchanged, record drift, emit event — Req 10.3
- Post-finalization: update docs or record drift for each changed module — Req 10.4
- `document` build mode: diff all docs against twin, update mismatched — Req 10.5
- Skip files already matching twin in document mode — Req 10.6
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


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers / fakes
# ─────────────────────────────────────────────────────────────────────────────


class FakeDocWriter:
    """Fake DocWriter that records calls and returns configurable results."""

    def __init__(
        self,
        *,
        results: list[DocUpdateResult] | None = None,
        should_fail: bool = False,
        fail_error: str = "DocWriter unavailable",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results = results or []
        self.should_fail = should_fail
        self.fail_error = fail_error

    async def update_docs(
        self, diff: TwinDiff, doc_files: list[DocFile]
    ) -> list[DocUpdateResult]:
        self.calls.append({"diff": diff, "doc_files": doc_files})
        if self.should_fail:
            raise RuntimeError(self.fail_error)
        if self.results:
            return self.results
        # Default: succeed for all doc files
        return [
            DocUpdateResult(doc_file=f.path, success=True, new_content="updated")
            for f in doc_files
        ]


class FakeTwinState:
    """Fake TwinStateProvider for testing."""

    def __init__(
        self,
        doc_files: list[DocFile] | None = None,
        matching_files: set[str] | None = None,
    ) -> None:
        self._doc_files = doc_files or []
        self._matching_files = matching_files or set()

    def get_doc_files(self) -> list[DocFile]:
        return self._doc_files

    def is_doc_matching_twin(self, doc_file: DocFile) -> bool:
        return doc_file.path in self._matching_files


class FakeEventEmitter:
    """Captures emitted events."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


# ─────────────────────────────────────────────────────────────────────────────
# Tests: compute_twin_diff_from_twins utility (Req 10.1)
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeTwinDiffFromTwins:
    """Tests for the standalone twin diff computation utility — Req 10.1."""

    def test_no_changes(self) -> None:
        """Diff of identical file sets has no changes."""
        files = {"src/main.py", "src/utils.py"}
        diff = compute_twin_diff_from_twins(files, files)
        assert diff.is_empty
        assert "no changes" in diff.summary.lower()

    def test_files_added(self) -> None:
        """New files appear as added changes."""
        prior = {"src/main.py"}
        current = {"src/main.py", "src/new_module.py"}
        diff = compute_twin_diff_from_twins(prior, current)
        assert diff.files_added == ["src/new_module.py"]
        assert not diff.is_empty

    def test_files_removed(self) -> None:
        """Removed files appear as removed changes."""
        prior = {"src/main.py", "src/old.py"}
        current = {"src/main.py"}
        diff = compute_twin_diff_from_twins(prior, current)
        assert diff.files_removed == ["src/old.py"]

    def test_files_modified_from_changed_files(self) -> None:
        """Modified files identified from explicit changed_files list."""
        files = {"src/main.py", "src/utils.py"}
        diff = compute_twin_diff_from_twins(
            files, files, changed_files=["src/utils.py"]
        )
        assert diff.files_modified == ["src/utils.py"]

    def test_combined_changes(self) -> None:
        """Diff captures added, removed, and modified simultaneously."""
        prior = {"src/main.py", "src/old.py", "src/utils.py"}
        current = {"src/main.py", "src/utils.py", "src/new.py"}
        diff = compute_twin_diff_from_twins(
            prior, current, changed_files=["src/utils.py"]
        )
        assert diff.files_added == ["src/new.py"]
        assert diff.files_removed == ["src/old.py"]
        assert diff.files_modified == ["src/utils.py"]

    def test_summary_describes_changes(self) -> None:
        """Summary string describes the change counts."""
        prior: set[str] = {"a.py"}
        current = {"a.py", "b.py", "c.py"}
        diff = compute_twin_diff_from_twins(prior, current)
        assert "2 added" in diff.summary

    def test_commit_sha_carried(self) -> None:
        """Commit SHA is included in the diff and change details."""
        diff = compute_twin_diff_from_twins(
            set(), {"new.py"}, commit_sha="abc123"
        )
        assert diff.commit_sha == "abc123"
        assert diff.all_changes[0].details["commit_sha"] == "abc123"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: TwinDiff model
# ─────────────────────────────────────────────────────────────────────────────


class TestTwinDiffModel:
    """Tests for the TwinDiff data model."""

    def test_is_empty_with_no_changes(self) -> None:
        """is_empty is True when there are no changes."""
        diff = TwinDiff()
        assert diff.is_empty
        assert not diff.has_changes
        assert "no changes" in diff.summary.lower()

    def test_is_empty_false_with_changes(self) -> None:
        """is_empty is False when there are changes."""
        diff = TwinDiff(changes=[ModuleChange(path="a.py")])
        assert not diff.is_empty
        assert diff.has_changes

    def test_all_changes_returns_copy(self) -> None:
        """all_changes returns a copy of the changes list."""
        changes = [ModuleChange(path="a.py")]
        diff = TwinDiff(changes=changes)
        result = diff.all_changes
        assert result == changes
        assert result is not diff.changes

    def test_modules_added_constructor(self) -> None:
        """TwinDiff can be constructed with modules_added/changed/removed."""
        diff = TwinDiff(
            modules_added=[ModuleChange("a.py", "added")],
            modules_changed=[ModuleChange("b.py", "changed")],
            modules_removed=[ModuleChange("c.py", "removed")],
        )
        assert not diff.is_empty
        assert len(diff.all_changes) == 3
        assert "2 added" not in diff.summary  # 1 added
        assert "1 added" in diff.summary


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DocumentationMaintenance.compute_twin_diff (Req 10.1)
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeTwinDiff:
    """Tests for DocumentationMaintenance.compute_twin_diff — Req 10.1."""

    def test_computes_diff_from_changed_files(self) -> None:
        """Req 10.1: Computes twin diff from changed files."""
        maintenance = DocumentationMaintenance(session_id="sess-1")
        diff = maintenance.compute_twin_diff(
            commit_sha="abc123",
            changed_files=["src/main.py", "src/utils.py"],
        )
        assert diff.commit_sha == "abc123"
        assert diff.session_id == "sess-1"
        assert not diff.is_empty
        assert len(diff.all_changes) == 2

    def test_empty_diff_for_no_changes(self) -> None:
        """Req 10.1: Empty diff when no files changed."""
        maintenance = DocumentationMaintenance(session_id="sess-1")
        diff = maintenance.compute_twin_diff(
            commit_sha="abc123",
            changed_files=[],
        )
        assert diff.is_empty

    def test_diffs_are_tracked(self) -> None:
        """Multiple twin diffs are tracked across commits."""
        maintenance = DocumentationMaintenance(session_id="sess-1")
        maintenance.compute_twin_diff("sha1", ["a.py"])
        maintenance.compute_twin_diff("sha2", ["b.py"])
        assert len(maintenance.twin_diffs) == 2

    def test_summary_describes_changes(self) -> None:
        """Req 10.1: Twin diff summary is human-readable."""
        maintenance = DocumentationMaintenance(session_id="sess-1")
        diff = maintenance.compute_twin_diff(
            commit_sha="abc123",
            changed_files=["src/new.py"],
        )
        assert "changed" in diff.summary.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DocumentationMaintenance.on_commit_done (Req 10.2, 10.3)
# ─────────────────────────────────────────────────────────────────────────────


class TestOnCommitDone:
    """Tests for commit.done handling — Req 10.2, 10.3."""

    @pytest.fixture
    def doc_files(self) -> list[DocFile]:
        return [
            DocFile(path="README.md", content="# Project"),
            DocFile(path="docs/API.md", content="# API"),
        ]

    @pytest.fixture
    def twin_state(self, doc_files: list[DocFile]) -> FakeTwinState:
        return FakeTwinState(doc_files=doc_files)

    @pytest.fixture
    def event_emitter(self) -> FakeEventEmitter:
        return FakeEventEmitter()

    @pytest.mark.asyncio
    async def test_doc_writer_invoked_on_commit_done(
        self, twin_state: FakeTwinState, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.2: DocWriter is invoked when commit.done triggers."""
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        diff, drift = await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        assert len(writer.calls) == 1
        call = writer.calls[0]
        assert call["diff"].commit_sha == "abc123"

    @pytest.mark.asyncio
    async def test_emits_doc_updated_event(
        self, twin_state: FakeTwinState, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.2: Emits doc_updated event with files and summary."""
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        doc_events = event_emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(doc_events) == 1
        assert "twin_diff_summary" in doc_events[0].payload
        assert doc_events[0].payload["commit_sha"] == "abc123"

    @pytest.mark.asyncio
    async def test_no_writer_call_when_no_changes(
        self, twin_state: FakeTwinState, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.2: DocWriter is not invoked when diff is empty."""
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=[],
        )

        assert len(writer.calls) == 0

    @pytest.mark.asyncio
    async def test_records_drift_on_writer_exception(
        self, twin_state: FakeTwinState, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.3: On DocWriter failure, record drift and emit event."""
        writer = FakeDocWriter(should_fail=True, fail_error="AI provider down")
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        diff, drift = await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        # Drift was recorded
        assert len(drift) > 0
        assert any("doc_writer_error" in e.reason for e in drift)

        # Failure event emitted as ERROR with error_type=doc_drift
        error_events = event_emitter.events_of_type(EventType.ERROR)
        assert len(error_events) == 1
        assert error_events[0].payload["error_type"] == "doc_drift"

    @pytest.mark.asyncio
    async def test_records_drift_when_no_writer_available(
        self, twin_state: FakeTwinState, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.3: Records drift when no DocWriter is configured."""
        maintenance = DocumentationMaintenance(
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        diff, drift = await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        assert len(drift) > 0
        assert any("no_doc_writer_available" in e.reason for e in drift)

    @pytest.mark.asyncio
    async def test_updated_files_tracked(
        self, twin_state: FakeTwinState, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.2: Successfully updated files are tracked."""
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        assert len(maintenance.updated_files) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DocumentationMaintenance.on_finalization (Req 10.4)
# ─────────────────────────────────────────────────────────────────────────────


class TestOnFinalization:
    """Tests for post-finalization drift check — Req 10.4."""

    @pytest.fixture
    def event_emitter(self) -> FakeEventEmitter:
        return FakeEventEmitter()

    @pytest.mark.asyncio
    async def test_records_drift_for_uncovered_modules(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.4: Records drift for changed modules without updated docs."""
        maintenance = DocumentationMaintenance(
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        # Simulate a commit with changes
        maintenance.compute_twin_diff("sha1", ["src/new.py", "src/main.py"])
        # Simulate that only src/main.py was successfully updated
        maintenance._updated_files.append("src/main.py")

        drift = await maintenance.finalize_documentation()

        assert len(drift) == 1
        assert drift[0].module_path == "src/new.py"
        assert "module_changed_but_docs_not_updated" in drift[0].reason

    @pytest.mark.asyncio
    async def test_no_drift_when_all_updated(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.4: No drift entries when all changed modules had docs updated."""
        maintenance = DocumentationMaintenance(
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        maintenance.compute_twin_diff("sha1", ["src/main.py"])
        maintenance._updated_files.append("src/main.py")

        drift = await maintenance.finalize_documentation()

        assert drift == []

    @pytest.mark.asyncio
    async def test_emits_drift_event_on_finalization(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.4: Emits event carrying drift entries after finalization."""
        maintenance = DocumentationMaintenance(
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        maintenance.compute_twin_diff("sha1", ["src/new.py"])

        await maintenance.finalize_documentation()

        error_events = event_emitter.events_of_type(EventType.ERROR)
        drift_events = [
            e for e in error_events
            if e.payload.get("error_type") == "doc_drift"
        ]
        assert len(drift_events) == 1
        assert drift_events[0].payload["total_drift_count"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DocumentationMaintenance.run_document_mode (Req 10.5, 10.6)
# ─────────────────────────────────────────────────────────────────────────────


class TestDocumentMode:
    """Tests for `document` build mode — Req 10.5, 10.6."""

    @pytest.fixture
    def event_emitter(self) -> FakeEventEmitter:
        return FakeEventEmitter()

    @pytest.mark.asyncio
    async def test_updates_mismatched_docs(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.5: Updates documentation files that don't match the twin."""
        doc_files = [
            DocFile(path="docs/api.md", content="old"),
            DocFile(path="docs/guide.md", content="old"),
        ]
        twin_state = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        updated, drift = await maintenance.run_document_mode()

        assert len(updated) == 2
        assert len(writer.calls) == 1

    @pytest.mark.asyncio
    async def test_skips_matching_docs(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.6: Skips documentation files that already match the twin."""
        doc_files = [
            DocFile(path="docs/api.md", content="current"),
            DocFile(path="docs/guide.md", content="old"),
        ]
        twin_state = FakeTwinState(
            doc_files=doc_files, matching_files={"docs/api.md"}
        )
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        updated, drift = await maintenance.run_document_mode()

        # Only guide.md should be updated
        assert updated == ["docs/guide.md"]
        assert len(drift) == 0

    @pytest.mark.asyncio
    async def test_all_docs_match_no_writer_call(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.6: No writer call when all docs already match."""
        doc_files = [
            DocFile(path="docs/api.md", content="current"),
        ]
        twin_state = FakeTwinState(
            doc_files=doc_files, matching_files={"docs/api.md"}
        )
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        updated, drift = await maintenance.run_document_mode()

        assert updated == []
        assert drift == []
        assert len(writer.calls) == 0

    @pytest.mark.asyncio
    async def test_records_drift_when_writer_fails(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.3/10.5: Records drift on writer failure in document mode."""
        doc_files = [DocFile(path="docs/api.md", content="old")]
        twin_state = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter(should_fail=True, fail_error="Service unavailable")
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        updated, drift = await maintenance.run_document_mode()

        assert updated == []
        assert len(drift) == 1
        assert drift[0].module_path == "docs/api.md"

    @pytest.mark.asyncio
    async def test_no_writer_records_drift(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Records drift for all mismatched files when no writer available."""
        doc_files = [
            DocFile(path="docs/api.md", content="old"),
            DocFile(path="docs/guide.md", content="old"),
        ]
        twin_state = FakeTwinState(doc_files=doc_files, matching_files=set())
        maintenance = DocumentationMaintenance(
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        updated, drift = await maintenance.run_document_mode()

        assert updated == []
        assert len(drift) == 2

    @pytest.mark.asyncio
    async def test_emits_doc_updated_in_document_mode(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.2: Emits doc_updated event in document mode on success."""
        doc_files = [DocFile(path="docs/api.md", content="old")]
        twin_state = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="sess-1",
        )

        await maintenance.run_document_mode()

        doc_events = event_emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(doc_events) == 1
        assert "docs/api.md" in doc_events[0].payload["files"]
