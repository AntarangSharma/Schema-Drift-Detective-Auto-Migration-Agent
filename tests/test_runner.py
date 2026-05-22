"""Tests for ``WatcherRunner.run_once`` — orchestration of one poll cycle."""

from __future__ import annotations

from schema_drift.models import (
    ChangeType,
    ColumnSpec,
    SchemaSnapshot,
    SourceKind,
    TableSnapshot,
)
from schema_drift.runner import WatcherRunner
from schema_drift.storage import InMemorySnapshotStore
from schema_drift.watcher.base import SourceWatcher

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _col(name: str, ordinal: int = 1) -> ColumnSpec:
    return ColumnSpec(name=name, data_type="text", nullable=True, ordinal_position=ordinal)


def _snap(columns: tuple[ColumnSpec, ...]) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_kind=SourceKind.POSTGRES,
        source_identifier="pg",
        tables=(TableSnapshot(table_identifier="source_raw.orders", columns=columns),),
    )


class _ScriptedWatcher(SourceWatcher):
    """Returns scripted snapshots in order; raises if asked once too many."""

    def __init__(self, snapshots: list[SchemaSnapshot]) -> None:
        self._snapshots = list(snapshots)
        self._calls = 0

    def snapshot(self) -> SchemaSnapshot:
        if self._calls >= len(self._snapshots):
            raise AssertionError("watcher.snapshot() called more times than scripted")
        snap = self._snapshots[self._calls]
        self._calls += 1
        return snap


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunOnce:
    def test_first_call_returns_baseline_no_events(self):
        snap = _snap((_col("id"),))
        store = InMemorySnapshotStore()
        runner = WatcherRunner(watcher=_ScriptedWatcher([snap]), store=store)

        result = runner.run_once()

        assert result.is_baseline is True
        assert result.events == []
        assert result.event_count == 0
        # Baseline must still persist so the *next* call has something to diff.
        assert store.latest("pg") == snap

    def test_second_call_with_no_changes_returns_zero_events(self):
        snap = _snap((_col("id"),))
        store = InMemorySnapshotStore()
        runner = WatcherRunner(
            watcher=_ScriptedWatcher([snap, snap]),
            store=store,
        )

        runner.run_once()  # baseline
        result = runner.run_once()

        assert result.is_baseline is False
        assert result.event_count == 0

    def test_second_call_after_nullable_add_yields_one_event(self):
        before = _snap((_col("id"),))
        after = _snap((_col("id"), _col("discount_code", ordinal=2)))
        store = InMemorySnapshotStore()
        runner = WatcherRunner(
            watcher=_ScriptedWatcher([before, after]),
            store=store,
        )

        runner.run_once()  # baseline
        result = runner.run_once()

        assert result.is_baseline is False
        assert result.event_count == 1
        ev = result.events[0]
        assert ev.change_type is ChangeType.COLUMN_ADDED_NULLABLE
        assert ev.source_identifier == "source_raw.orders.discount_code"

    def test_run_persists_every_snapshot(self):
        before = _snap((_col("id"),))
        after = _snap((_col("id"), _col("discount_code", ordinal=2)))
        store = InMemorySnapshotStore()
        runner = WatcherRunner(
            watcher=_ScriptedWatcher([before, after]),
            store=store,
        )

        runner.run_once()
        runner.run_once()

        # Both snapshots end up in the store under the same source.
        assert store.count("pg") == 2
        assert store.latest("pg") == after

    def test_classifier_returning_none_filters_event(self):
        # Stub-classifier path: explicitly return None for every input so
        # we exercise the runner's "filter None events" branch independent
        # of whatever the production classifier currently supports. (The
        # original test relied on 'removed' being unhandled, which is no
        # longer true after the Week-2 classifier expansion.)
        from schema_drift.classifier import Classifier

        class _AlwaysNoneClassifier(Classifier):
            def classify(self, raw_change):  # type: ignore[override]
                return None

        before = _snap((_col("id"), _col("doomed", ordinal=2)))
        after = _snap((_col("id"),))
        store = InMemorySnapshotStore()
        runner = WatcherRunner(
            watcher=_ScriptedWatcher([before, after]),
            store=store,
            classifier=_AlwaysNoneClassifier(),
        )

        runner.run_once()
        result = runner.run_once()
        assert result.is_baseline is False
        assert result.events == []
