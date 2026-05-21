"""Persistence layer for snapshots and drift events.

Exposes a thin ``SnapshotStore`` protocol so the runner can be unit-tested
against an in-memory implementation without a live Postgres.
"""

from schema_drift.storage.snapshot_store import (
    InMemorySnapshotStore,
    PostgresSnapshotStore,
    SnapshotStore,
)

__all__ = [
    "InMemorySnapshotStore",
    "PostgresSnapshotStore",
    "SnapshotStore",
]
