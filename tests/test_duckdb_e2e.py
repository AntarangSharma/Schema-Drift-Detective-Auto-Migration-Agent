"""DuckDB end-to-end test.

Boots an in-memory DuckDB, creates two snapshots (before + after a
schema change), feeds them through:
* ``SourceWatcher.diff`` (the base, watcher-agnostic differ)
* ``Classifier.classify_batch`` (the rule classifier)

…and asserts the right ``ChangeType`` lands. This is the cheapest
e2e gate we have — no Postgres container, no network.

Skipped automatically if ``duckdb`` is not installed.
"""

from __future__ import annotations

import pytest

from schema_drift.classifier import Classifier
from schema_drift.models import ChangeType
from schema_drift.watcher.base import SourceWatcher

duckdb = pytest.importorskip("duckdb")

from schema_drift.watcher.duckdb import DuckDBWatcher, DuckDBWatcherConfig  # noqa: E402


def _setup_orders(conn) -> None:
    conn.execute("DROP SCHEMA IF EXISTS demo CASCADE")
    conn.execute("CREATE SCHEMA demo")
    conn.execute(
        """
        CREATE TABLE demo.orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount DECIMAL(10, 2)
        )
        """
    )


class TestDuckDBWatcher:
    def test_snapshot_emits_expected_table(self) -> None:
        conn = duckdb.connect(":memory:")
        _setup_orders(conn)
        w = DuckDBWatcher(DuckDBWatcherConfig(schema="demo"), connection=conn)
        snap = w.snapshot()
        idents = {t.table_identifier for t in snap.tables}
        assert "demo.orders" in idents
        orders = next(t for t in snap.tables if t.table_identifier == "demo.orders")
        col_names = {c.name for c in orders.columns}
        assert col_names == {"id", "customer_id", "amount"}

    def test_diff_detects_column_added(self) -> None:
        conn = duckdb.connect(":memory:")
        _setup_orders(conn)
        w = DuckDBWatcher(DuckDBWatcherConfig(schema="demo"), connection=conn)
        before = w.snapshot()
        conn.execute("ALTER TABLE demo.orders ADD COLUMN email VARCHAR")
        after = w.snapshot()

        raws = SourceWatcher.diff(before, after)
        added = [r for r in raws if r.kind == "added"]
        assert len(added) == 1
        assert added[0].column_after is not None
        assert added[0].column_after.name == "email"

    def test_e2e_drop_column_classified_as_column_dropped(self) -> None:
        conn = duckdb.connect(":memory:")
        _setup_orders(conn)
        w = DuckDBWatcher(DuckDBWatcherConfig(schema="demo"), connection=conn)
        before = w.snapshot()
        conn.execute("ALTER TABLE demo.orders DROP COLUMN amount")
        after = w.snapshot()

        raws = SourceWatcher.diff(before, after)
        events = Classifier().classify_batch(raws)
        # Exactly one event, exactly the right ChangeType.
        assert len(events) == 1
        assert events[0].change_type is ChangeType.COLUMN_DROPPED
        # Severity floor enforced (HIGH for column_dropped).
        assert events[0].severity.value == "high"

    def test_e2e_nullable_add_classified(self) -> None:
        conn = duckdb.connect(":memory:")
        _setup_orders(conn)
        w = DuckDBWatcher(DuckDBWatcherConfig(schema="demo"), connection=conn)
        before = w.snapshot()
        conn.execute("ALTER TABLE demo.orders ADD COLUMN notes VARCHAR")
        after = w.snapshot()

        raws = SourceWatcher.diff(before, after)
        events = Classifier().classify_batch(raws)
        assert len(events) == 1
        assert events[0].change_type is ChangeType.COLUMN_ADDED_NULLABLE
