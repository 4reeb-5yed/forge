"""Property-based tests for documentation non-drift using Hypothesis.

**Validates: Requirements 10.1, 10.2, 10.3, 10.4**

Property 10: Documentation non-drift — after finalize, for every module changed
    in the twin diff, either docs were updated or a DocDrift entry is recorded.

This ensures documentation never silently drifts from code changes without at
least being tracked. The property validates the invariant that:
- After finalization completes
- For every module that appeared in the twin diff (added, changed, or removed)
- EITHER a `doc_updated` event was emitted for that module
- OR a drift entry was recorded (indicating the Doc_Writer could not update)

No module may "fall through the cracks" — every change is either documented or
explicitly tracked as drifted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st, assume

from app.runtime.events.models import Event, EventType
from app.runtime.types import DocUpdateResult, TwinDiff


# ---------------------------------------------------------------------------
# Domain model for the property test
# ---------------------------------------------------------------------------


@dataclass
class DocDriftEntry:
    """Records that a module's documentation could not be updated.

    Requirement 10.3: If Doc_Writer cannot update a doc file, leave file
    unchanged and record a drift entry.
    Requirement 10.4: After finalization, for each changed module, either
    update docs or record drift.
    """

    module: str
    reason: str
    session_id: str = ""


@dataclass
class DocumentationMaintenanceResult:
    """Result of running documentation maintenance after finalization.

    Tracks which modules had their docs successfully updated and which
    had drift entries recorded.
    """

    updated_modules: set[str] = field(default_factory=set)
    drift_entries: list[DocDriftEntry] = field(default_factory=list)
    events_emitted: list[Event] = field(default_factory=list)

    @property
    def drifted_modules(self) -> set[str]:
        """Set of modules that have drift entries."""
        return {entry.module for entry in self.drift_entries}


# ---------------------------------------------------------------------------
# System under test: DocumentationMaintenance
# ---------------------------------------------------------------------------


class FakeDocWriter:
    """A configurable Doc_Writer that succeeds or fails per module.

    Simulates the behavior where some modules can be documented and
    others cannot (network errors, model failures, complex changes, etc.).
    """

    def __init__(self, failing_modules: set[str] | None = None) -> None:
        """
        Args:
            failing_modules: Set of module names for which update_docs will fail.
                If None, all modules succeed.
        """
        self._failing_modules = failing_modules or set()
        self.name = "fake-doc-writer"

    async def update_docs_for_module(
        self, module: str, diff: TwinDiff
    ) -> DocUpdateResult:
        """Attempt to update docs for a single module.

        Returns success or failure based on configuration.
        """
        if module in self._failing_modules:
            return DocUpdateResult(
                success=False,
                error=f"Failed to update documentation for module '{module}'",
            )
        return DocUpdateResult(
            success=True,
            files_updated=[f"docs/{module}.md"],
        )


class DocumentationMaintenance:
    """Orchestrates documentation updates after finalization.

    For every module changed in the twin diff:
    - Attempts to update documentation via the Doc_Writer
    - On success: emits a `doc_updated` event
    - On failure: records a DocDrift entry and emits an event

    This guarantees the non-drift invariant: no module silently drifts.

    Requirements:
        10.1 — Compute twin diff on commit.done
        10.2 — DocWriter updates docs; emits doc_updated
        10.3 — On failure: leave file unchanged, record drift, emit event
        10.4 — After finalization: update or record drift for each module
    """

    def __init__(
        self,
        doc_writer: FakeDocWriter,
        session_id: str = "",
    ) -> None:
        self._doc_writer = doc_writer
        self._session_id = session_id

    async def process_after_finalization(
        self, twin_diff: TwinDiff
    ) -> DocumentationMaintenanceResult:
        """Process documentation for all modules changed in the twin diff.

        For each module in the diff (added, modified, removed), attempts
        doc update and records drift on failure. This ensures every module
        is either documented or explicitly tracked.

        Args:
            twin_diff: The computed diff from the Digital Twin.

        Returns:
            Result with updated modules, drift entries, and emitted events.
        """
        result = DocumentationMaintenanceResult()

        # Collect all changed modules from the twin diff (Req 10.1)
        changed_modules = set(
            twin_diff.files_added
            + twin_diff.files_modified
            + twin_diff.files_removed
        )

        for module in changed_modules:
            # Attempt documentation update (Req 10.2)
            doc_result = await self._doc_writer.update_docs_for_module(
                module, twin_diff
            )

            if doc_result.success:
                # Doc successfully updated — emit doc_updated event (Req 10.2)
                result.updated_modules.add(module)
                event = Event.create(
                    type=EventType.DOC_UPDATED,
                    session_id=self._session_id,
                    source="documentation_maintenance",
                    payload={
                        "module": module,
                        "files_updated": doc_result.files_updated,
                        "files_created": doc_result.files_created,
                    },
                )
                result.events_emitted.append(event)
            else:
                # Doc update failed — record drift entry (Req 10.3)
                drift = DocDriftEntry(
                    module=module,
                    reason=doc_result.error,
                    session_id=self._session_id,
                )
                result.drift_entries.append(drift)
                # Emit drift event (Req 10.3)
                event = Event.create(
                    type=EventType.ERROR,
                    session_id=self._session_id,
                    source="documentation_maintenance",
                    payload={
                        "error_type": "doc_drift",
                        "module": module,
                        "reason": doc_result.error,
                    },
                )
                result.events_emitted.append(event)

        return result


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy for generating valid module names (simulating Python module paths)
module_name_st = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_/",
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "" and not s.startswith("/") and not s.endswith("/"))

# Strategy for generating a set of unique module names
module_set_st = st.lists(
    module_name_st,
    min_size=0,
    max_size=10,
    unique=True,
)


@st.composite
def twin_diff_st(draw: st.DrawFn) -> TwinDiff:
    """Generate arbitrary TwinDiff with non-overlapping added/modified/removed sets."""
    all_modules = draw(st.lists(module_name_st, min_size=1, max_size=15, unique=True))

    # Partition modules into added, modified, removed (non-overlapping)
    n = len(all_modules)
    # Each module randomly assigned to one category
    categories = draw(
        st.lists(
            st.sampled_from(["added", "modified", "removed"]),
            min_size=n,
            max_size=n,
        )
    )

    added = [m for m, c in zip(all_modules, categories) if c == "added"]
    modified = [m for m, c in zip(all_modules, categories) if c == "modified"]
    removed = [m for m, c in zip(all_modules, categories) if c == "removed"]

    return TwinDiff(
        files_added=added,
        files_modified=modified,
        files_removed=removed,
        summary=f"Changes: +{len(added)} ~{len(modified)} -{len(removed)}",
    )


@st.composite
def failing_modules_st(draw: st.DrawFn, all_modules: list[str]) -> set[str]:
    """Generate a subset of modules that will fail doc update.

    Can range from no failures to all failures.
    """
    if not all_modules:
        return set()

    # Each module independently may fail
    failure_flags = draw(
        st.lists(st.booleans(), min_size=len(all_modules), max_size=len(all_modules))
    )
    return {m for m, fails in zip(all_modules, failure_flags) if fails}


# ---------------------------------------------------------------------------
# Property 10: Documentation non-drift
# ---------------------------------------------------------------------------


class TestDocumentationNonDriftProperty:
    """Property 10: Documentation non-drift — after finalize, for every module
    changed in the twin diff, either docs were updated or a DocDrift entry is
    recorded.

    **Validates: Requirements 10.1, 10.2, 10.3, 10.4**
    """

    @given(data=st.data())
    @settings(max_examples=200, deadline=10000)
    def test_every_changed_module_either_updated_or_drifted(
        self, data: st.DataObject
    ) -> None:
        """For any set of changed modules and any pattern of doc-writer
        successes/failures, after finalization every module in the twin diff
        is either in the updated set or has a drift entry recorded.

        No module may silently drift — the union of updated and drifted
        modules must equal exactly the set of all changed modules.
        """
        # Generate a twin diff with arbitrary changed modules
        diff = data.draw(twin_diff_st(), label="twin_diff")

        # Compute all changed modules
        all_changed = set(diff.files_added + diff.files_modified + diff.files_removed)
        assume(len(all_changed) > 0)

        # Generate arbitrary failure pattern
        failing = data.draw(failing_modules_st(list(all_changed)), label="failing_modules")

        # Run documentation maintenance
        doc_writer = FakeDocWriter(failing_modules=failing)
        maintenance = DocumentationMaintenance(
            doc_writer=doc_writer,
            session_id="test-session",
        )

        result = asyncio.run(maintenance.process_after_finalization(diff))

        # THE PROPERTY: every changed module is either updated or drifted
        covered_modules = result.updated_modules | result.drifted_modules

        assert covered_modules == all_changed, (
            f"Documentation non-drift violated!\n"
            f"Changed modules: {all_changed}\n"
            f"Updated modules: {result.updated_modules}\n"
            f"Drifted modules: {result.drifted_modules}\n"
            f"Missing coverage: {all_changed - covered_modules}\n"
            f"Extra coverage: {covered_modules - all_changed}"
        )

    @given(data=st.data())
    @settings(max_examples=200, deadline=10000)
    def test_no_module_both_updated_and_drifted(
        self, data: st.DataObject
    ) -> None:
        """For any twin diff and failure pattern, a module must appear in
        exactly one of {updated} or {drifted}, never both.

        This ensures the categorization is clean: each module has a single
        definitive outcome.
        """
        diff = data.draw(twin_diff_st(), label="twin_diff")

        all_changed = set(diff.files_added + diff.files_modified + diff.files_removed)
        assume(len(all_changed) > 0)

        failing = data.draw(failing_modules_st(list(all_changed)), label="failing_modules")

        doc_writer = FakeDocWriter(failing_modules=failing)
        maintenance = DocumentationMaintenance(
            doc_writer=doc_writer,
            session_id="test-session",
        )

        result = asyncio.run(maintenance.process_after_finalization(diff))

        # No overlap between updated and drifted
        overlap = result.updated_modules & result.drifted_modules
        assert overlap == set(), (
            f"Module(s) appear in both updated and drifted sets: {overlap}\n"
            f"Updated: {result.updated_modules}\n"
            f"Drifted: {result.drifted_modules}"
        )

    @given(data=st.data())
    @settings(max_examples=200, deadline=10000)
    def test_doc_updated_event_emitted_for_each_successful_update(
        self, data: st.DataObject
    ) -> None:
        """For every module whose docs were successfully updated, a doc_updated
        event must have been emitted carrying that module's identifier.

        Validates Requirement 10.2: DocWriter updates docs and emits doc_updated.
        """
        diff = data.draw(twin_diff_st(), label="twin_diff")

        all_changed = set(diff.files_added + diff.files_modified + diff.files_removed)
        assume(len(all_changed) > 0)

        failing = data.draw(failing_modules_st(list(all_changed)), label="failing_modules")

        doc_writer = FakeDocWriter(failing_modules=failing)
        maintenance = DocumentationMaintenance(
            doc_writer=doc_writer,
            session_id="test-session",
        )

        result = asyncio.run(maintenance.process_after_finalization(diff))

        # Every updated module should have a doc_updated event
        doc_updated_events = [
            e for e in result.events_emitted if e.type == EventType.DOC_UPDATED
        ]
        event_modules = {e.payload["module"] for e in doc_updated_events}

        assert event_modules == result.updated_modules, (
            f"doc_updated events don't match updated modules!\n"
            f"Updated modules: {result.updated_modules}\n"
            f"Event modules: {event_modules}\n"
            f"Missing events for: {result.updated_modules - event_modules}\n"
            f"Extra events for: {event_modules - result.updated_modules}"
        )

    @given(data=st.data())
    @settings(max_examples=200, deadline=10000)
    def test_drift_event_emitted_for_each_failed_update(
        self, data: st.DataObject
    ) -> None:
        """For every module where doc update failed, an event must be emitted
        carrying the failure reason and module identifier.

        Validates Requirement 10.3: On failure, record drift entry and emit event.
        """
        diff = data.draw(twin_diff_st(), label="twin_diff")

        all_changed = set(diff.files_added + diff.files_modified + diff.files_removed)
        assume(len(all_changed) > 0)

        # Ensure at least one failure to make this test meaningful
        failing = data.draw(
            failing_modules_st(list(all_changed)).filter(lambda s: len(s) > 0),
            label="failing_modules",
        )
        assume(len(failing) > 0)

        doc_writer = FakeDocWriter(failing_modules=failing)
        maintenance = DocumentationMaintenance(
            doc_writer=doc_writer,
            session_id="test-session",
        )

        result = asyncio.run(maintenance.process_after_finalization(diff))

        # Every drifted module should have a drift event
        drift_events = [
            e for e in result.events_emitted
            if e.type == EventType.ERROR and e.payload.get("error_type") == "doc_drift"
        ]
        drift_event_modules = {e.payload["module"] for e in drift_events}

        assert drift_event_modules == result.drifted_modules, (
            f"Drift events don't match drifted modules!\n"
            f"Drifted modules: {result.drifted_modules}\n"
            f"Drift event modules: {drift_event_modules}\n"
            f"Missing events for: {result.drifted_modules - drift_event_modules}\n"
            f"Extra events for: {drift_event_modules - result.drifted_modules}"
        )

    @given(data=st.data())
    @settings(max_examples=150, deadline=10000)
    def test_successful_modules_match_non_failing_set(
        self, data: st.DataObject
    ) -> None:
        """The set of successfully updated modules must exactly equal the set
        of changed modules minus the failing modules.

        This validates that the Doc_Writer's success/failure directly determines
        whether a module gets updated vs. recorded as drifted.
        """
        diff = data.draw(twin_diff_st(), label="twin_diff")

        all_changed = set(diff.files_added + diff.files_modified + diff.files_removed)
        assume(len(all_changed) > 0)

        failing = data.draw(failing_modules_st(list(all_changed)), label="failing_modules")

        doc_writer = FakeDocWriter(failing_modules=failing)
        maintenance = DocumentationMaintenance(
            doc_writer=doc_writer,
            session_id="test-session",
        )

        result = asyncio.run(maintenance.process_after_finalization(diff))

        # Modules that should succeed = all_changed - failing
        expected_updated = all_changed - failing
        expected_drifted = all_changed & failing

        assert result.updated_modules == expected_updated, (
            f"Updated modules don't match expected!\n"
            f"Expected updated: {expected_updated}\n"
            f"Actual updated: {result.updated_modules}"
        )
        assert result.drifted_modules == expected_drifted, (
            f"Drifted modules don't match expected!\n"
            f"Expected drifted: {expected_drifted}\n"
            f"Actual drifted: {result.drifted_modules}"
        )

    @given(diff=twin_diff_st())
    @settings(max_examples=100, deadline=10000)
    def test_all_modules_succeed_when_no_failures(
        self, diff: TwinDiff
    ) -> None:
        """When the Doc_Writer never fails, ALL changed modules must be in the
        updated set and NO drift entries should exist.

        This is the happy path: full documentation coverage with no drift.
        """
        all_changed = set(diff.files_added + diff.files_modified + diff.files_removed)
        assume(len(all_changed) > 0)

        # No failures configured
        doc_writer = FakeDocWriter(failing_modules=set())
        maintenance = DocumentationMaintenance(
            doc_writer=doc_writer,
            session_id="test-session",
        )

        result = asyncio.run(maintenance.process_after_finalization(diff))

        assert result.updated_modules == all_changed, (
            f"All modules should be updated when no failures.\n"
            f"Expected: {all_changed}\n"
            f"Actual: {result.updated_modules}"
        )
        assert len(result.drift_entries) == 0, (
            f"No drift entries expected when all updates succeed.\n"
            f"Got: {result.drift_entries}"
        )

    @given(diff=twin_diff_st())
    @settings(max_examples=100, deadline=10000)
    def test_all_modules_drift_when_all_fail(
        self, diff: TwinDiff
    ) -> None:
        """When the Doc_Writer fails for ALL modules, every changed module must
        have a drift entry and the updated set must be empty.

        This is the worst case: full documentation drift, all tracked.
        """
        all_changed = set(diff.files_added + diff.files_modified + diff.files_removed)
        assume(len(all_changed) > 0)

        # All modules fail
        doc_writer = FakeDocWriter(failing_modules=all_changed)
        maintenance = DocumentationMaintenance(
            doc_writer=doc_writer,
            session_id="test-session",
        )

        result = asyncio.run(maintenance.process_after_finalization(diff))

        assert result.updated_modules == set(), (
            f"No modules should be updated when all fail.\n"
            f"Got updated: {result.updated_modules}"
        )
        assert result.drifted_modules == all_changed, (
            f"All modules should be drifted when all fail.\n"
            f"Expected: {all_changed}\n"
            f"Actual drifted: {result.drifted_modules}"
        )
