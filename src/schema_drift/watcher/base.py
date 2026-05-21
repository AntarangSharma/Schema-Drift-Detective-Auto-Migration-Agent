"""Abstract base for all source watchers.

Every concrete watcher implements two operations:

1. ``snapshot()`` — capture the current schema state as a ``SchemaSnapshot``.
2. ``diff(prev, curr)`` — compute the per-column delta between two snapshots
   as a list of ``RawChange`` objects (pre-classifier).

The classifier later turns ``RawChange`` streams into typed ``DriftEvent``s.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from schema_drift.models import RawChange, SchemaSnapshot


class SourceWatcher(ABC):
    """Common interface for Postgres / REST / Debezium / Kafka watchers."""

    @abstractmethod
    def snapshot(self) -> SchemaSnapshot:
        """Capture the current schema state of the monitored source."""

    @staticmethod
    def diff(prev: SchemaSnapshot, curr: SchemaSnapshot) -> list[RawChange]:
        """Compute table+column-level RawChanges between two snapshots.

        Pure function, watcher-agnostic — exposed as a staticmethod so the
        runtime loop can diff without holding a watcher instance.

        Detection rules
        ---------------
        * A table present in ``curr`` but not ``prev`` → ``table_added``.
        * A table present in ``prev`` but not ``curr`` → ``table_removed``.
        * For tables in both:
            - column in ``curr`` but not ``prev`` → ``added``
            - column in ``prev`` but not ``curr`` → ``removed``
            - column in both with any difference → ``modified``
        """
        changes: list[RawChange] = []
        prev_tables = {t.table_identifier: t for t in prev.tables}
        curr_tables = {t.table_identifier: t for t in curr.tables}

        for ident in sorted(curr_tables.keys() - prev_tables.keys()):
            changes.append(RawChange(table_identifier=ident, kind="table_added"))
        for ident in sorted(prev_tables.keys() - curr_tables.keys()):
            changes.append(RawChange(table_identifier=ident, kind="table_removed"))

        for ident in sorted(prev_tables.keys() & curr_tables.keys()):
            prev_cols = {c.name: c for c in prev_tables[ident].columns}
            curr_cols = {c.name: c for c in curr_tables[ident].columns}

            for name in sorted(curr_cols.keys() - prev_cols.keys()):
                changes.append(
                    RawChange(table_identifier=ident, kind="added", column_after=curr_cols[name])
                )
            for name in sorted(prev_cols.keys() - curr_cols.keys()):
                changes.append(
                    RawChange(table_identifier=ident, kind="removed", column_before=prev_cols[name])
                )
            for name in sorted(prev_cols.keys() & curr_cols.keys()):
                if prev_cols[name] != curr_cols[name]:
                    changes.append(
                        RawChange(
                            table_identifier=ident,
                            kind="modified",
                            column_before=prev_cols[name],
                            column_after=curr_cols[name],
                        )
                    )
        return changes
