"""Documentation Maintenance - Digital Twin diff and doc synchronization.

Handles computing twin diffs on commit.done events and updating documentation
files via the DocWriter capability. Tracks documentation drift when updates
fail, and supports the `document` build mode for full doc synchronization.

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, Protocol

from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DocFile:
    """A documentation file tracked by the Digital Twin's documentation state."""

    path: str
    content: str = ""
    last_synced_sha: str = ""


@dataclass
class DocUpdateResult:
    """Result of updating a single documentation file."""

    doc_file: str
    success: bool
    new_content: str = ""
    error: str = ""


@dataclass
class ModuleChange:
    """A single module change within a twin diff."""

    path: str
    change_type: str = "modified"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocDriftEntry:
    """A record of documentation that drifted from the code."""

    module_path: str = ""
    doc_file: str = ""
    reason: str = ""
    commit_sha: str = ""
    session_id: str = ""
    file_path: str = ""
    module: str = ""


@dataclass
class TwinDiff:
    """A diff computed from the Digital Twin describing modules added/changed/removed."""

    commit_sha: str = ""
    session_id: str = ""
    changes: list[ModuleChange] = field(default_factory=list)
    summary: str = ""
    modules_added: list[ModuleChange] = field(default_factory=list)
    modules_changed: list[ModuleChange] = field(default_factory=list)
    modules_removed: list[ModuleChange] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.changes and (
            self.modules_added or self.modules_changed or self.modules_removed
        ):
            merged: list[ModuleChange] = []
            for mc in self.modules_added:
                merged.append(ModuleChange(path=mc.path, change_type="added", details=mc.details))
            for mc in self.modules_changed:
                merged.append(ModuleChange(path=mc.path, change_type="changed", details=mc.details))
            for mc in self.modules_removed:
                merged.append(ModuleChange(path=mc.path, change_type="removed", details=mc.details))
            self.changes = merged

        if not self.summary:
            if not self.changes:
                self.summary = "no changes"
            else:
                parts: list[str] = []
                added = [c for c in self.changes if c.change_type == "added"]
                changed = [c for c in self.changes if c.change_type in ("changed", "modified")]
                removed = [c for c in self.changes if c.change_type == "removed"]
                if added:
                    parts.append(f"{len(added)} added")
                if changed:
                    parts.append(f"{len(changed)} changed")
                if removed:
                    parts.append(f"{len(removed)} removed")
                self.summary = ", ".join(parts) if parts else "no changes"

    @property
    def is_empty(self) -> bool:
        return len(self.changes) == 0

    @property
    def all_changes(self) -> list[ModuleChange]:
        return list(self.changes)

    @property
    def files_added(self) -> list[str]:
        return [c.path for c in self.changes if c.change_type == "added"]

    @property
    def files_removed(self) -> list[str]:
        return [c.path for c in self.changes if c.change_type == "removed"]

    @property
    def files_modified(self) -> list[str]:
        return [c.path for c in self.changes if c.change_type in ("modified", "changed")]

    @property
    def has_changes(self) -> bool:
        return not self.is_empty


class CommitDoneResult:
    """Result of on_commit_done that supports both tuple unpacking and attribute access.

    Can be used as:
        diff, drift = await maintenance.on_commit_done(...)
    Or:
        result = await maintenance.on_commit_done(...)
        result.commit_sha  # delegates to the TwinDiff
    """

    def __init__(self, diff: TwinDiff, drift: list[DocDriftEntry]) -> None:
        self._diff = diff
        self._drift = drift

    def __iter__(self) -> Iterator[Any]:
        yield self._diff
        yield self._drift

    # Delegate TwinDiff attributes
    @property
    def commit_sha(self) -> str:
        return self._diff.commit_sha

    @property
    def session_id(self) -> str:
        return self._diff.session_id

    @property
    def is_empty(self) -> bool:
        return self._diff.is_empty

    @property
    def all_changes(self) -> list[ModuleChange]:
        return self._diff.all_changes

    @property
    def summary(self) -> str:
        return self._diff.summary

    @property
    def has_changes(self) -> bool:
        return self._diff.has_changes

    @property
    def drift(self) -> list[DocDriftEntry]:
        return self._drift


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class DocWriterProtocol(Protocol):
    async def update_docs(self, diff: TwinDiff, doc_files: list[DocFile]) -> list[DocUpdateResult]: ...


class TwinStateProvider(Protocol):
    def get_doc_files(self) -> list[DocFile]: ...
    def is_doc_matching_twin(self, doc_file: DocFile) -> bool: ...


EventEmitter = Callable[[Event], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Standalone utility
# ---------------------------------------------------------------------------


def compute_twin_diff_from_twins(
    prior_file_index: set[str],
    current_file_index: set[str],
    changed_files: list[str] | None = None,
    commit_sha: str = "",
    session_id: str = "",
) -> TwinDiff:
    """Compute a TwinDiff from file index sets."""
    changes: list[ModuleChange] = []

    for path in sorted(current_file_index - prior_file_index):
        changes.append(ModuleChange(path=path, change_type="added", details={"commit_sha": commit_sha}))

    for path in sorted(prior_file_index - current_file_index):
        changes.append(ModuleChange(path=path, change_type="removed", details={"commit_sha": commit_sha}))

    if changed_files is not None:
        common = prior_file_index & current_file_index
        for path in sorted(changed_files):
            if path in common:
                changes.append(ModuleChange(path=path, change_type="modified", details={"commit_sha": commit_sha}))

    parts: list[str] = []
    n_added = sum(1 for c in changes if c.change_type == "added")
    n_modified = sum(1 for c in changes if c.change_type == "modified")
    n_removed = sum(1 for c in changes if c.change_type == "removed")
    if n_added:
        parts.append(f"{n_added} added")
    if n_modified:
        parts.append(f"{n_modified} changed")
    if n_removed:
        parts.append(f"{n_removed} removed")
    summary = ", ".join(parts) if parts else "no changes"

    return TwinDiff(commit_sha=commit_sha, session_id=session_id, changes=changes, summary=summary)


# ---------------------------------------------------------------------------
# DocumentationMaintenance
# ---------------------------------------------------------------------------


class DocumentationMaintenance:
    """Maintains documentation in sync with code via Digital Twin diffs.

    Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6
    """

    def __init__(
        self,
        *,
        doc_writer: Any | None = None,
        twin_state: Any | None = None,
        event_emitter: Any | None = None,
        session_id: str = "",
        workspace_path: str = "",
    ) -> None:
        self._doc_writer = doc_writer
        self._twin_state = twin_state
        self._event_emitter = event_emitter
        self._session_id = session_id
        self._workspace_path = workspace_path
        self._drift_entries: list[DocDriftEntry] = []
        self._twin_diffs: list[TwinDiff] = []
        self._updated_files: list[str] = []

    @property
    def drift_entries(self) -> list[DocDriftEntry]:
        return list(self._drift_entries)

    @property
    def twin_diffs(self) -> list[TwinDiff]:
        return list(self._twin_diffs)

    @property
    def updated_files(self) -> list[str]:
        return list(self._updated_files)

    # -- Req 10.1 --

    def compute_twin_diff(self, commit_sha: str, changed_files: list[str]) -> TwinDiff:
        """Compute a twin diff describing modules added, changed, or removed."""
        changes = [
            ModuleChange(path=path, change_type="changed", details={"commit_sha": commit_sha})
            for path in changed_files
        ]
        summary = f"{len(changes)} changed module(s) in commit {commit_sha[:8]}" if changes else "no changes"
        diff = TwinDiff(commit_sha=commit_sha, session_id=self._session_id, changes=changes, summary=summary)
        self._twin_diffs.append(diff)
        return diff

    # -- Req 10.2, 10.3 --

    async def on_commit_done(self, commit_sha: str, changed_files: list[str]) -> CommitDoneResult:
        """Handle a commit.done event. Returns CommitDoneResult (supports tuple unpacking)."""
        drift_before = len(self._drift_entries)
        diff = self.compute_twin_diff(commit_sha, changed_files)

        if diff.is_empty:
            return CommitDoneResult(diff, [])

        doc_files = self._get_doc_files()
        if not doc_files:
            return CommitDoneResult(diff, [])

        if self._doc_writer is not None:
            await self._update_docs_with_writer(diff, doc_files, commit_sha)
        else:
            for change in diff.all_changes:
                self._record_drift(module_path=change.path, reason="no_doc_writer_available", commit_sha=commit_sha)

        new_drift = self._drift_entries[drift_before:]
        return CommitDoneResult(diff, list(new_drift))

    # -- Req 10.4 --

    async def on_finalization(self) -> list[DocDriftEntry]:
        """After finalization, record drift for undocumented changed modules."""
        finalization_drift: list[DocDriftEntry] = []
        all_changed: set[str] = set()
        for diff in self._twin_diffs:
            for change in diff.all_changes:
                all_changed.add(change.path)

        updated_set = set(self._updated_files)
        for module in sorted(all_changed):
            if module not in updated_set:
                entry = DocDriftEntry(
                    module_path=module, reason="module_changed_but_docs_not_updated",
                    session_id=self._session_id, file_path=module, module=module,
                )
                finalization_drift.append(entry)
                self._drift_entries.append(entry)

        if finalization_drift:
            await self._emit_drift_event(finalization_drift)

        return finalization_drift

    async def finalize_documentation(self) -> list[DocDriftEntry]:
        """Alias for on_finalization."""
        return await self.on_finalization()

    # -- Req 10.5, 10.6 --

    async def run_document_mode(self) -> list[DocUpdateResult]:
        """Sync all docs against the twin in document build mode.

        Returns list of DocUpdateResult for files that were processed.
        Empty list if nothing to update or on failure.
        """
        doc_files = self._get_doc_files()
        if not doc_files:
            return []

        files_to_update: list[DocFile] = []
        for doc_file in doc_files:
            if not self._is_doc_matching_twin(doc_file):
                files_to_update.append(doc_file)

        if not files_to_update:
            return []

        diff = TwinDiff(
            session_id=self._session_id,
            changes=[ModuleChange(path=f.path, change_type="modified") for f in files_to_update],
            summary=f"{len(files_to_update)} documentation files need update",
        )

        if self._doc_writer is None:
            for doc_file in files_to_update:
                self._record_drift(
                    module_path=doc_file.path, doc_file_path=doc_file.path,
                    reason="no_doc_writer_available_for_document_mode",
                )
            return []

        try:
            results = await self._doc_writer.update_docs(diff, files_to_update)
        except Exception as exc:
            for doc_file in files_to_update:
                self._record_drift(
                    module_path=doc_file.path, doc_file_path=doc_file.path,
                    reason=f"doc_writer_failed: {exc}",
                )
            await self._emit_doc_failure_event(files=[f.path for f in files_to_update], reason=str(exc))
            return []

        for result in results:
            if result.success:
                self._updated_files.append(result.doc_file)
            else:
                self._record_drift(
                    module_path=result.doc_file, doc_file_path=result.doc_file,
                    reason=f"doc_writer_partial_failure: {result.error}",
                )

        successful_files = [r.doc_file for r in results if r.success]
        if successful_files:
            await self._emit_doc_updated_event(files=successful_files, diff_summary=diff.summary)

        return results

    # -- Internal helpers --

    def _get_doc_files(self) -> list[DocFile]:
        if self._twin_state is not None and hasattr(self._twin_state, "get_doc_files"):
            return self._twin_state.get_doc_files()
        return []

    def _is_doc_matching_twin(self, doc_file: DocFile) -> bool:
        if self._twin_state is not None and hasattr(self._twin_state, "is_doc_matching_twin"):
            return self._twin_state.is_doc_matching_twin(doc_file)
        return False

    async def _update_docs_with_writer(self, diff: TwinDiff, doc_files: list[DocFile], commit_sha: str) -> None:
        try:
            results = await self._doc_writer.update_docs(diff, doc_files)
        except Exception as exc:
            logger.error("DocWriter failed for session %s: %s", self._session_id, exc)
            for doc_file in doc_files:
                self._record_drift(module_path=doc_file.path, doc_file_path=doc_file.path, reason=f"doc_writer_error: {exc}", commit_sha=commit_sha)
            await self._emit_doc_failure_event(files=[f.path for f in doc_files], reason=str(exc))
            return

        successful_files: list[str] = []
        for result in results:
            if result.success:
                successful_files.append(result.doc_file)
                self._updated_files.append(result.doc_file)
            else:
                self._record_drift(module_path=result.doc_file, doc_file_path=result.doc_file, reason=f"doc_writer_partial_failure: {result.error}", commit_sha=commit_sha)

        if successful_files:
            await self._emit_doc_updated_event(files=successful_files, diff_summary=diff.summary, commit_sha=commit_sha)

    def _record_drift(self, module_path: str = "", doc_file_path: str = "", reason: str = "", commit_sha: str = "") -> DocDriftEntry:
        entry = DocDriftEntry(
            module_path=module_path, doc_file=doc_file_path, reason=reason,
            commit_sha=commit_sha, session_id=self._session_id,
            file_path=module_path, module=module_path,
        )
        self._drift_entries.append(entry)
        logger.info("Documentation drift recorded for %s: %s", module_path, reason)
        return entry

    # -- Event emission --

    async def _emit_event(self, event: Event) -> None:
        if self._event_emitter is None:
            return
        try:
            if hasattr(self._event_emitter, "emit"):
                await self._event_emitter.emit(event)
            elif callable(self._event_emitter):
                await self._event_emitter(event)
        except Exception:
            logger.exception("Failed to emit event: %s", event.type)

    async def _emit_doc_updated_event(self, files: list[str], diff_summary: str, commit_sha: str = "") -> None:
        payload: dict[str, Any] = {"files": files, "twin_diff_summary": diff_summary}
        if commit_sha:
            payload["commit_sha"] = commit_sha
        event = Event.create(
            type=EventType.DOC_UPDATED, session_id=self._session_id,
            source="documentation_maintenance", payload=payload,
            correlation_id=self._session_id,
            event_id=f"doc-updated-{self._session_id}-{uuid.uuid4().hex[:8]}",
        )
        await self._emit_event(event)

    async def _emit_doc_failure_event(self, files: list[str], reason: str) -> None:
        event = Event.create(
            type=EventType.DOC_DRIFT, session_id=self._session_id,
            source="documentation_maintenance",
            payload={
                "files": files, "reason": reason,
                "drift_entries": [
                    {"file_path": e.module_path, "reason": e.reason}
                    for e in self._drift_entries if e.module_path in files
                ],
            },
            correlation_id=self._session_id,
            event_id=f"doc-drift-{self._session_id}-{uuid.uuid4().hex[:8]}",
        )
        await self._emit_event(event)

    async def _emit_drift_event(self, drift_entries: list[DocDriftEntry]) -> None:
        event = Event.create(
            type=EventType.DOC_DRIFT, session_id=self._session_id,
            source="documentation_maintenance",
            payload={
                "drift_entries": [{"module_path": e.module_path, "reason": e.reason} for e in drift_entries],
                "total_drift_count": len(drift_entries),
            },
            correlation_id=self._session_id,
            event_id=f"doc-finalize-drift-{self._session_id}-{uuid.uuid4().hex[:8]}",
        )
        await self._emit_event(event)
