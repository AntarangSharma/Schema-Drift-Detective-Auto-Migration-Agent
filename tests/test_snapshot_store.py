"""Tests for the snapshot persistence layer.

Only the ``InMemorySnapshotStore`` is exercised here — the Postgres-backed
store is covered by the integration suite (Day 5+, gated by docker-compose).
That split is deliberate: unit tests must stay hermetic + fast.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schema_drift.models import (
    ColumnSpec,
    SchemaSnapshot,
    SourceKind,
    TableSnapshot,
)
from schema_drift.storage import InMemorySnapshotStore


def _snap(
    *,
    source_identifier: str = "pg",
    captured_at: datetime | None = None,
    snapshot_id: str | None = None,
) -> SchemaSnapshot:
    fields: dict = {
        "source_kind": SourceKind.POSTGRES,
        "source_identifier": source_identifier,
        "tables": (
            TableSnapshot(
                table_identifier="source_raw.orders",
                columns=(
                    ColumnSpec(name="id", data_type="int8", nullable=False, ordinal_position=1),
                ),
            ),
        ),
    }
    if captured_at is not None:
        fields["captured_at"] = captured_at
    if snapshot_id is not None:
        fields["snapshot_id"] = snapshot_id
    return SchemaSnapshot(**fields)


class TestInMemoryStore:
    def test_empty_store_returns_none(self):
        store = InMemorySnapshotStore()
        assert store.latest("pg") is None
        assert store.count() == 0

    def test_save_then_latest(self):
        store = InMemorySnapshotStore()
        snap = _snap()
        store.save(snap)
        assert store.latest("pg") == snap
        assert store.count("pg") == 1
        assert store.count() == 1

    def test_latest_picks_most_recent_by_captured_at(self):
        store = InMemorySnapshotStore()
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        older = _snap(captured_at=t0)
        newer = _snap(captured_at=t0 + timedelta(seconds=10))
        store.save(older)
        store.save(newer)
        assert store.latest("pg") == newer

    def test_save_is_idempotent_on_snapshot_id(self):
        store = InMemorySnapshotStore()
        snap = _snap(snapshot_id="01HJTEST")
        store.save(snap)
        store.save(snap)  # second save must not duplicate
        assert store.count("pg") == 1

    def test_separate_sources_dont_collide(self):
        store = InMemorySnapshotStore()
        store.save(_snap(source_identifier="pg-a"))
        store.save(_snap(source_identifier="pg-b"))
        assert store.count() == 2
        assert store.count("pg-a") == 1
        assert store.latest("pg-a") is not None
        assert store.latest("pg-b") is not None
        assert store.latest("pg-a") != store.latest("pg-b")

    def test_unknown_source_returns_none(self):
        store = InMemorySnapshotStore()
        store.save(_snap(source_identifier="pg-a"))
        assert store.latest("pg-z") is None
        assert store.count("pg-z") == 0


class TestSnapshotRoundTrip:
    """Pin the JSON wire format. If any of these drift, downstream readers break."""

    def test_round_trip_via_model_json(self):
        snap = _snap()
        restored = SchemaSnapshot.model_validate_json(snap.model_dump_json())
        assert restored == snap

    def test_round_trip_via_model_dump_json_mode(self):
        # ``mode="json"`` is what the Postgres store uses for the JSONB payload.
        snap = _snap()
        restored = SchemaSnapshot.model_validate(snap.model_dump(mode="json"))
        assert restored == snap
