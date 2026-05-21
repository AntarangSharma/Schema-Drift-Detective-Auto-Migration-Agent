"""Tests for ``SourceWatcher.diff`` — the pure snapshot-delta function."""

from __future__ import annotations

from schema_drift.models import (
    ColumnSpec,
    SchemaSnapshot,
    SourceKind,
    TableSnapshot,
)
from schema_drift.watcher.base import SourceWatcher


def _col(name: str, **over) -> ColumnSpec:
    base = {
        "name": name,
        "data_type": "text",
        "nullable": True,
        "ordinal_position": 1,
    }
    base.update(over)
    return ColumnSpec(**base)


def _snap(*tables: TableSnapshot) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_kind=SourceKind.POSTGRES,
        source_identifier="pg",
        tables=tables,
    )


class TestDiff:
    def test_no_changes_yields_empty(self):
        t = TableSnapshot(table_identifier="s.t", columns=(_col("a"),))
        assert SourceWatcher.diff(_snap(t), _snap(t)) == []

    def test_added_column(self):
        before = TableSnapshot(table_identifier="s.t", columns=(_col("a"),))
        after = TableSnapshot(
            table_identifier="s.t",
            columns=(_col("a"), _col("b", ordinal_position=2)),
        )
        changes = SourceWatcher.diff(_snap(before), _snap(after))
        assert len(changes) == 1
        assert changes[0].kind == "added"
        assert changes[0].column_after is not None
        assert changes[0].column_after.name == "b"

    def test_removed_column(self):
        before = TableSnapshot(
            table_identifier="s.t",
            columns=(_col("a"), _col("b", ordinal_position=2)),
        )
        after = TableSnapshot(table_identifier="s.t", columns=(_col("a"),))
        changes = SourceWatcher.diff(_snap(before), _snap(after))
        assert len(changes) == 1
        assert changes[0].kind == "removed"
        assert changes[0].column_before is not None
        assert changes[0].column_before.name == "b"

    def test_modified_column(self):
        before = TableSnapshot(table_identifier="s.t", columns=(_col("a"),))
        after = TableSnapshot(table_identifier="s.t", columns=(_col("a", data_type="varchar"),))
        changes = SourceWatcher.diff(_snap(before), _snap(after))
        assert len(changes) == 1
        assert changes[0].kind == "modified"
        assert changes[0].column_before is not None
        assert changes[0].column_after is not None

    def test_table_added_and_removed(self):
        a = TableSnapshot(table_identifier="s.a", columns=(_col("x"),))
        b = TableSnapshot(table_identifier="s.b", columns=(_col("x"),))
        changes = SourceWatcher.diff(_snap(a), _snap(b))
        kinds = sorted(c.kind for c in changes)
        assert kinds == ["table_added", "table_removed"]
