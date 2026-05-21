"""Live integration tests against the docker-compose Postgres.

These tests are auto-skipped when the database isn't reachable so the unit
suite stays hermetic. Run the full live suite with::

    make up   # docker-compose Postgres
    pytest -m integration

The marker is declared in ``pyproject.toml``.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from schema_drift.classifier import Classifier
from schema_drift.runner import WatcherRunner
from schema_drift.storage import PostgresSnapshotStore
from schema_drift.watcher.postgres import PostgresWatcher

DSN = os.getenv("DRIFT_DSN", "postgresql://drift:drift@localhost:55432/drift")


def _postgres_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=1) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except psycopg.OperationalError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _postgres_available(),
        reason="docker-compose Postgres not reachable on $DRIFT_DSN",
    ),
]


# ---------------------------------------------------------------------------
# A unique source_identifier per test run keeps the schema_snapshots table
# from filling with stale rows when the suite runs repeatedly.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_source(request) -> str:
    return f"itest-{request.node.name}-{os.getpid()}"


class TestPostgresSnapshotStore:
    def test_save_and_latest_round_trip(self, isolated_source: str) -> None:
        watcher = PostgresWatcher(
            dsn=DSN, schemas=("source_raw",), source_identifier=isolated_source
        )
        store = PostgresSnapshotStore(dsn=DSN)

        snap = watcher.snapshot()
        store.save(snap)
        restored = store.latest(isolated_source)

        assert restored is not None
        assert restored.source_identifier == isolated_source
        assert restored.tables == snap.tables

    def test_latest_for_unknown_source_returns_none(self) -> None:
        store = PostgresSnapshotStore(dsn=DSN)
        assert store.latest("does-not-exist") is None


class TestWatcherRunnerAgainstLiveDB:
    def test_first_run_is_baseline_zero_events(self, isolated_source: str) -> None:
        runner = WatcherRunner(
            watcher=PostgresWatcher(
                dsn=DSN, schemas=("source_raw",), source_identifier=isolated_source
            ),
            store=PostgresSnapshotStore(dsn=DSN),
            classifier=Classifier(),
        )
        result = runner.run_once()
        assert result.is_baseline is True
        assert result.event_count == 0

    def test_second_run_with_no_changes_yields_zero_events(self, isolated_source: str) -> None:
        store = PostgresSnapshotStore(dsn=DSN)
        runner = WatcherRunner(
            watcher=PostgresWatcher(
                dsn=DSN, schemas=("source_raw",), source_identifier=isolated_source
            ),
            store=store,
        )
        runner.run_once()  # baseline
        result = runner.run_once()
        assert result.is_baseline is False
        assert result.event_count == 0
