"""Unit tests for commit, finalization, and documentation maintenance.

Comprehensive tests covering the integration of:
- CommitWorkflow (Requirements 9.1-9.5)
- FinalizationWorkflow (Requirements 9.3, 9.6, 9.7)
- DocumentationMaintenance (Requirements 10.1-10.6)

This file adds coverage for:
- Twin diff computation and doc update on commit.done
- Drift recording on DocWriter failure
- Document mode behavior (diff all docs against twin)
- Integration between commit → finalization → documentation flows
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.runtime.documentation import (
    DocDriftEntry,
    DocFile,
    DocUpdateResult,
    DocumentationMaintenance,
    ModuleChange,
    TwinDiff,
)
from app.runtime.events.models import Event, EventType


# ─────────────────────────────────────────────────────────────────────────────
# Test Helpers
# ─────────────────────────────────────────────────────────────────────────────


class FakeDocWriter:
    """Fake DocWriter that can succeed or fail."""

    def __init__(
        self,
        results: list[DocUpdateResult] | None = None,
        should_fail: bool = False,
        fail_error: str = "DocWriter unavailable",
    ) -> None:
        self.results = results
        self.should_fail = should_fail
        self.fail_error = fail_error
        self.calls: list[dict[str, Any]] = []

    async def update_docs(
        self, diff: TwinDiff, doc_files: list[DocFile]
    ) -> list[DocUpdateResult]:
        self.calls.append({"diff": diff, "doc_files": doc_files})
        if self.should_fail:
            raise RuntimeError(self.fail_error)
        if self.results is not None:
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
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def doc_files() -> list[DocFile]:
    return [
        DocFile(path="README.md", content="# Project", last_synced_sha="aaa"),
        DocFile(path="docs/API.md", content="# API", last_synced_sha="aaa"),
        DocFile(path="docs/ARCHITECTURE.md", content="# Arch", last_synced_sha="aaa"),
    ]


@pytest.fixture
def fake_twin_state(doc_files: list[DocFile]) -> FakeTwinState:
    return FakeTwinState(doc_files=doc_files)


@pytest.fixture
def fake_doc_writer() -> FakeDocWriter:
    return FakeDocWriter()


@pytest.fixture
def event_emitter() -> FakeEventEmitter:
    return FakeEventEmitter()


@pytest.fixture
def doc_maintenance(
    fake_doc_writer: FakeDocWriter,
    fake_twin_state: FakeTwinState,
    event_emitter: FakeEventEmitter,
) -> DocumentationMaintenance:
    return DocumentationMaintenance(
        doc_writer=fake_doc_writer,
        twin_state=fake_twin_state,
        event_emitter=event_emitter,
        session_id="session-1",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test: Twin diff computation (Req 10.1)
# ─────────────────────────────────────────────────────────────────────────────


class TestTwinDiffComputation:
    """Tests for computing twin diff on commit.done (Req 10.1)."""

    def test_compute_twin_diff_classifies_changed_files(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Req 10.1: Twin diff describes modules changed."""
        diff = doc_maintenance.compute_twin_diff(
            commit_sha="abc123",
            changed_files=["src/main.py", "src/utils.py"],
        )

        assert diff.commit_sha == "abc123"
        assert diff.session_id == "session-1"
        assert not diff.is_empty
        assert len(diff.all_changes) == 2

    def test_compute_twin_diff_empty_for_no_changes(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Req 10.1: Empty diff when no files changed."""
        diff = doc_maintenance.compute_twin_diff(
            commit_sha="abc123", changed_files=[]
        )

        assert diff.is_empty
        assert diff.all_changes == []

    def test_twin_diff_summary_describes_changes(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Req 10.1: Twin diff summary is human-readable."""
        diff = doc_maintenance.compute_twin_diff(
            commit_sha="abc123",
            changed_files=["src/new.py", "src/changed.py"],
        )

        assert diff.summary != ""
        assert "2" in diff.summary  # should mention count

    def test_twin_diff_tracks_commit_sha_in_details(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Req 10.1: Each change carries the commit SHA in details."""
        diff = doc_maintenance.compute_twin_diff(
            commit_sha="def456", changed_files=["a.py"]
        )

        assert diff.commit_sha == "def456"
        for change in diff.all_changes:
            assert change.details["commit_sha"] == "def456"

    def test_twin_diffs_are_accumulated(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Multiple twin diffs are tracked across commits."""
        doc_maintenance.compute_twin_diff("sha1", ["a.py"])
        doc_maintenance.compute_twin_diff("sha2", ["b.py"])

        assert len(doc_maintenance.twin_diffs) == 2

    def test_twin_diff_dataclass_empty(self) -> None:
        """TwinDiff reports empty correctly."""
        diff = TwinDiff()
        assert diff.is_empty
        assert diff.all_changes == []
        assert diff.has_changes is False

    def test_twin_diff_dataclass_with_changes(self) -> None:
        """TwinDiff with changes reports correctly."""
        diff = TwinDiff(
            changes=[
                ModuleChange(path="a.py", change_type="added"),
                ModuleChange(path="b.py", change_type="modified"),
                ModuleChange(path="c.py", change_type="removed"),
            ]
        )
        assert not diff.is_empty
        assert diff.has_changes
        assert len(diff.all_changes) == 3
        assert diff.files_added == ["a.py"]
        assert diff.files_modified == ["b.py"]
        assert diff.files_removed == ["c.py"]


# ─────────────────────────────────────────────────────────────────────────────
# Test: DocWriter updates docs on commit (Req 10.2)
# ─────────────────────────────────────────────────────────────────────────────


class TestDocWriterUpdates:
    """Tests for DocWriter updating README and docs files (Req 10.2)."""

    @pytest.mark.asyncio
    async def test_doc_writer_invoked_on_commit_done(
        self,
        doc_maintenance: DocumentationMaintenance,
        fake_doc_writer: FakeDocWriter,
    ) -> None:
        """Req 10.2: DocWriter is invoked when commit.done triggers."""
        await doc_maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        assert len(fake_doc_writer.calls) == 1
        call = fake_doc_writer.calls[0]
        assert call["diff"].commit_sha == "abc123"

    @pytest.mark.asyncio
    async def test_doc_updated_event_emitted_on_success(
        self,
        doc_maintenance: DocumentationMaintenance,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.2: doc_updated event is emitted on successful update."""
        await doc_maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        doc_events = event_emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(doc_events) == 1
        payload = doc_events[0].payload
        assert "twin_diff_summary" in payload
        assert payload["commit_sha"] == "abc123"

    @pytest.mark.asyncio
    async def test_doc_updated_event_carries_files(
        self,
        doc_maintenance: DocumentationMaintenance,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.2: doc_updated event carries changed doc file paths."""
        await doc_maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        doc_events = event_emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(doc_events) == 1
        assert "files" in doc_events[0].payload
        assert len(doc_events[0].payload["files"]) > 0

    @pytest.mark.asyncio
    async def test_no_doc_writer_call_on_empty_diff(
        self,
        doc_maintenance: DocumentationMaintenance,
        fake_doc_writer: FakeDocWriter,
    ) -> None:
        """Req 10.2: DocWriter is not invoked when diff is empty."""
        await doc_maintenance.on_commit_done(
            commit_sha="abc123", changed_files=[]
        )

        assert len(fake_doc_writer.calls) == 0

    @pytest.mark.asyncio
    async def test_updated_files_tracked(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Req 10.2: Successfully updated files are tracked."""
        await doc_maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        assert len(doc_maintenance.updated_files) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Test: Drift recording on DocWriter failure (Req 10.3)
# ─────────────────────────────────────────────────────────────────────────────


class TestDriftRecording:
    """Tests for drift recording when DocWriter fails (Req 10.3)."""

    @pytest.mark.asyncio
    async def test_drift_recorded_on_complete_failure(
        self,
        fake_twin_state: FakeTwinState,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.3: When DocWriter fails entirely, drift is recorded."""
        failing_writer = FakeDocWriter(should_fail=True, fail_error="Service down")
        maintenance = DocumentationMaintenance(
            doc_writer=failing_writer,
            twin_state=fake_twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module_a.py", "src/module_b.py"],
        )

        drift = maintenance.drift_entries
        # Drift entries should cover the doc files that couldn't be updated
        assert len(drift) > 0
        assert all("Service down" in entry.reason for entry in drift)

    @pytest.mark.asyncio
    async def test_drift_recorded_on_partial_failure(
        self,
        fake_twin_state: FakeTwinState,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.3: When some doc updates fail, drift is recorded for those."""
        partial_writer = FakeDocWriter(
            results=[
                DocUpdateResult(doc_file="README.md", success=True, new_content="ok"),
                DocUpdateResult(doc_file="docs/API.md", success=False, error="parse error"),
            ]
        )
        maintenance = DocumentationMaintenance(
            doc_writer=partial_writer,
            twin_state=fake_twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        drift = maintenance.drift_entries
        assert len(drift) == 1
        assert drift[0].file_path == "docs/API.md"
        assert "parse error" in drift[0].reason

    @pytest.mark.asyncio
    async def test_file_unchanged_on_failure_drift_recorded(
        self,
        fake_twin_state: FakeTwinState,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.3: On failure, file content is left unchanged (drift instead)."""
        failing_writer = FakeDocWriter(should_fail=True)
        maintenance = DocumentationMaintenance(
            doc_writer=failing_writer,
            twin_state=fake_twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        # Drift was recorded instead of updating files
        assert len(maintenance.drift_entries) > 0
        assert len(maintenance.updated_files) == 0

    @pytest.mark.asyncio
    async def test_drift_event_emitted_on_failure(
        self,
        fake_twin_state: FakeTwinState,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.3: An event is emitted carrying the failure reason."""
        failing_writer = FakeDocWriter(should_fail=True, fail_error="timeout")
        maintenance = DocumentationMaintenance(
            doc_writer=failing_writer,
            twin_state=fake_twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        drift_events = event_emitter.events_of_type(EventType.DOC_DRIFT)
        assert len(drift_events) == 1
        payload = drift_events[0].payload
        assert "reason" in payload
        assert "timeout" in payload["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# Test: Finalization ensures docs or drift (Req 10.4)
# ─────────────────────────────────────────────────────────────────────────────


class TestFinalizationDocs:
    """Tests for post-finalization documentation guarantee (Req 10.4)."""

    @pytest.mark.asyncio
    async def test_finalize_creates_drift_for_undocumented_changes(
        self,
        fake_twin_state: FakeTwinState,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.4: After finalization, undocumented changes get drift entries."""
        # Use a writer that returns empty results = no updates
        writer = FakeDocWriter(results=[])
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=fake_twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        # Simulate a commit that changes modules
        await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module_a.py"],
        )

        # After finalization, ensure drift is recorded
        drift = await maintenance.on_finalization()
        assert len(drift) > 0

    @pytest.mark.asyncio
    async def test_finalize_emits_drift_event(
        self,
        fake_twin_state: FakeTwinState,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 10.4: Emit event carrying recorded drift entries."""
        writer = FakeDocWriter(results=[])
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=fake_twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        await maintenance.on_commit_done("abc123", ["src/module.py"])
        await maintenance.on_finalization()

        drift_events = event_emitter.events_of_type(EventType.DOC_DRIFT)
        assert len(drift_events) >= 1
        # Should have drift_entries in payload
        payload = drift_events[-1].payload
        assert "drift_entries" in payload
        assert payload["total_drift_count"] > 0

    @pytest.mark.asyncio
    async def test_finalize_no_drift_when_docs_updated(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Req 10.4: No extra drift when docs were successfully updated."""
        # Default FakeDocWriter succeeds for all doc files
        await doc_maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["README.md"],
        )

        drift = await doc_maintenance.on_finalization()

        # README.md should have been updated (it's in the doc files)
        # Check that updated modules don't appear in drift
        updated_set = set(doc_maintenance.updated_files)
        for entry in drift:
            assert entry.file_path not in updated_set


# ─────────────────────────────────────────────────────────────────────────────
# Test: Document build mode (Req 10.5, 10.6)
# ─────────────────────────────────────────────────────────────────────────────


class TestDocumentMode:
    """Tests for document build mode (Req 10.5, 10.6)."""

    @pytest.mark.asyncio
    async def test_document_mode_updates_mismatched_docs(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.5: Document mode updates files not matching twin."""
        doc_files = [
            DocFile(path="README.md", content="old content"),
            DocFile(path="docs/API.md", content="old api"),
        ]
        twin_state = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        results = await maintenance.run_document_mode()

        assert len(results) == 2
        assert all(r.success for r in results)
        assert len(maintenance.updated_files) == 2

    @pytest.mark.asyncio
    async def test_document_mode_skips_matching_files(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.6: Skip files already matching twin in document mode."""
        doc_files = [
            DocFile(path="README.md", content="current"),
            DocFile(path="docs/API.md", content="old"),
            DocFile(path="docs/SETUP.md", content="current"),
        ]
        # README.md and SETUP.md already match
        twin_state = FakeTwinState(
            doc_files=doc_files,
            matching_files={"README.md", "docs/SETUP.md"},
        )
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        results = await maintenance.run_document_mode()

        # Only docs/API.md should be processed
        assert len(results) == 1
        assert results[0].doc_file == "docs/API.md"
        assert results[0].success

    @pytest.mark.asyncio
    async def test_document_mode_all_matching_returns_empty(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.6: When all files match twin, returns empty list."""
        doc_files = [
            DocFile(path="README.md", content="ok"),
            DocFile(path="docs/API.md", content="ok"),
        ]
        twin_state = FakeTwinState(
            doc_files=doc_files,
            matching_files={"README.md", "docs/API.md"},
        )
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        results = await maintenance.run_document_mode()

        assert results == []
        assert len(writer.calls) == 0

    @pytest.mark.asyncio
    async def test_document_mode_records_drift_on_failure(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.5: Document mode records drift when DocWriter fails."""
        doc_files = [
            DocFile(path="README.md", content="old"),
            DocFile(path="docs/API.md", content="old"),
        ]
        twin_state = FakeTwinState(doc_files=doc_files, matching_files=set())
        failing_writer = FakeDocWriter(should_fail=True, fail_error="AI down")
        maintenance = DocumentationMaintenance(
            doc_writer=failing_writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        results = await maintenance.run_document_mode()

        assert results == []
        assert len(maintenance.drift_entries) == 2
        assert all("AI down" in entry.reason for entry in maintenance.drift_entries)

    @pytest.mark.asyncio
    async def test_document_mode_emits_doc_updated_event(
        self, event_emitter: FakeEventEmitter
    ) -> None:
        """Req 10.5: Document mode emits doc_updated event on success."""
        doc_files = [DocFile(path="README.md", content="old")]
        twin_state = FakeTwinState(doc_files=doc_files, matching_files=set())
        writer = FakeDocWriter()
        maintenance = DocumentationMaintenance(
            doc_writer=writer,
            twin_state=twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        await maintenance.run_document_mode()

        doc_events = event_emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(doc_events) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test: Integration — commit → documentation flow (Req 9.1, 10.1, 10.2)
# ─────────────────────────────────────────────────────────────────────────────


class TestCommitToDocIntegration:
    """Integration tests for commit → documentation flow."""

    @pytest.mark.asyncio
    async def test_commit_done_triggers_diff_and_doc_update(
        self,
        doc_maintenance: DocumentationMaintenance,
        fake_doc_writer: FakeDocWriter,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """Req 9.1 + 10.1 + 10.2: commit.done triggers diff then doc update."""
        diff, new_drift = await doc_maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/new_feature.py", "src/utils.py"],
        )

        # Diff was computed (10.1)
        assert diff.commit_sha == "abc123"
        assert len(diff.all_changes) == 2

        # DocWriter was invoked (10.2)
        assert len(fake_doc_writer.calls) == 1

        # doc_updated event was emitted (10.2)
        doc_events = event_emitter.events_of_type(EventType.DOC_UPDATED)
        assert len(doc_events) == 1

    @pytest.mark.asyncio
    async def test_multiple_commits_accumulate_diffs(
        self, doc_maintenance: DocumentationMaintenance
    ) -> None:
        """Multiple commit.done events accumulate twin diffs."""
        await doc_maintenance.on_commit_done("sha1", ["a.py"])
        await doc_maintenance.on_commit_done("sha2", ["b.py"])
        await doc_maintenance.on_commit_done("sha3", ["c.py"])

        assert len(doc_maintenance.twin_diffs) == 3

    @pytest.mark.asyncio
    async def test_no_event_emitter_still_works(
        self,
        fake_doc_writer: FakeDocWriter,
        fake_twin_state: FakeTwinState,
    ) -> None:
        """Documentation maintenance works without an event emitter."""
        maintenance = DocumentationMaintenance(
            doc_writer=fake_doc_writer,
            twin_state=fake_twin_state,
            event_emitter=None,
            session_id="session-1",
        )

        diff, new_drift = await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        assert diff.commit_sha == "abc123"
        assert len(fake_doc_writer.calls) == 1

    @pytest.mark.asyncio
    async def test_no_doc_writer_records_drift(
        self,
        fake_twin_state: FakeTwinState,
        event_emitter: FakeEventEmitter,
    ) -> None:
        """When no DocWriter is available, drift is recorded for all changes."""
        maintenance = DocumentationMaintenance(
            doc_writer=None,
            twin_state=fake_twin_state,
            event_emitter=event_emitter,
            session_id="session-1",
        )

        diff, new_drift = await maintenance.on_commit_done(
            commit_sha="abc123",
            changed_files=["src/module.py"],
        )

        assert len(maintenance.drift_entries) > 0
        assert "no_doc_writer" in maintenance.drift_entries[0].reason


# ─────────────────────────────────────────────────────────────────────────────
# Test: DocDriftEntry dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestDocDriftEntry:
    """Unit tests for DocDriftEntry data model."""

    def test_drift_entry_captures_all_fields(self) -> None:
        """Drift entry captures file_path, module, reason, commit_sha."""
        entry = DocDriftEntry(
            file_path="src/module.py",
            module="src/module.py",
            reason="Writer timeout",
            commit_sha="abc123",
        )

        assert entry.file_path == "src/module.py"
        assert entry.module == "src/module.py"
        assert entry.reason == "Writer timeout"
        assert entry.commit_sha == "abc123"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Commit verification gate (Req 9.1, 9.2) — additional coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestCommitVerificationGateAdditional:
    """Additional commit verification tests for Req 9.1, 9.2 coverage."""

    @pytest.mark.asyncio
    async def test_commit_requires_verification(self) -> None:
        """Req 9.2: A task must pass verify.passed before commit."""
        from app.runtime.commit import (
            CommitNotVerifiedError,
            CommitRequest,
            CommitWorkflow,
        )

        class FakeVCS:
            async def commit(self, repo_path: str, message: str, files: list[str]) -> str:
                return "sha"

        workflow = CommitWorkflow(
            vcs_committer=FakeVCS(),
            event_emitter=None,
            session_id="s1",
            canonical_repo_path="/repo",
        )

        request = CommitRequest(
            task_id="task-1",
            session_id="s1",
            workspace_path="/ws",
            changed_files=["x.py"],
        )

        with pytest.raises(CommitNotVerifiedError):
            await workflow.commit_task(request)


# ─────────────────────────────────────────────────────────────────────────────
# Test: Approval gate timeout (Req 9.7) — additional coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestApprovalTimeoutAdditional:
    """Additional tests for approval timeout behavior (Req 9.7)."""

    @pytest.mark.asyncio
    async def test_finalization_approval_timeout_no_push(self) -> None:
        """Req 9.7: No push when approval times out in finalization."""
        from app.runtime.finalization import FinalizationWorkflow

        class NoopPublisher:
            events: list[Event] = []
            async def publish(self, event: Event) -> Event:
                self.events.append(event)
                return event

        class NoopPusher:
            push_called = False
            async def push(self, repo_path: str) -> None:
                self.push_called = True

        class NoopAudit:
            def write_decision(self, **kwargs: Any) -> None:
                pass

        publisher = NoopPublisher()
        pusher = NoopPusher()

        workflow = FinalizationWorkflow(
            event_publisher=publisher,
            vcs_pusher=pusher,
            audit_writer=NoopAudit(),
            session_id="s1",
            canonical_path="/repo",
            approval_timeout=0.02,
            push_requires_approval=True,
        )

        result = await workflow.finalize(
            committed_tasks=["t1"],
            skipped_tasks=[],
            failed_tasks=[],
        )

        assert result is None
        assert pusher.push_called is False

    @pytest.mark.asyncio
    async def test_finalization_no_approval_proceeds(self) -> None:
        """Req 9.3: When approval not required, push proceeds immediately."""
        from app.runtime.finalization import FinalizationWorkflow

        class NoopPublisher:
            events: list[Event] = []
            async def publish(self, event: Event) -> Event:
                self.events.append(event)
                return event

        class NoopPusher:
            push_called = False
            async def push(self, repo_path: str) -> None:
                self.push_called = True

        class NoopAudit:
            def write_decision(self, **kwargs: Any) -> None:
                pass

        publisher = NoopPublisher()
        pusher = NoopPusher()

        workflow = FinalizationWorkflow(
            event_publisher=publisher,
            vcs_pusher=pusher,
            audit_writer=NoopAudit(),
            session_id="s1",
            canonical_path="/repo",
            push_requires_approval=False,
        )

        result = await workflow.finalize(
            committed_tasks=["t1", "t2"],
            skipped_tasks=["t3"],
            failed_tasks=[],
        )

        assert result is not None
        assert pusher.push_called is True
        assert result.committed_tasks == ["t1", "t2"]
