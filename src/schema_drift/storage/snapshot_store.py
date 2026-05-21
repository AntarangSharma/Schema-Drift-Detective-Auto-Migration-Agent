"""Snapshot persistence.

Three classes ship here:

* :class:`SnapshotStore` — the ``Protocol`` the rest of the pipeline targets.
* :class:`PostgresSnapshotStore` — production implementation backed by
  ``schema_drift.schema_snapshots`` (DDL in ``infra/postgres-init.sql``).
* :class:`InMemorySnapshotStore` — test stand-in. Pure Python, zero deps.

Wire format
-----------
Snapshots are persisted as JSON via :meth:`SchemaSnapshot.model_dump_json`,
so the on-disk format *is* the pydantic schema. Forward-compat is the
model's job; the store stays format-agnostic.
"""

from __future__ import annotations

from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from schema_drift.models import SchemaSnapshot

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class SnapshotStore(Protocol):
    """Anything the watcher loop needs from a snapshot store.

    Both ``save`` and ``latest`` key on ``source_identifier``; multiple sources
    can coexist in a single store.
    """

    def save(self, snapshot: SchemaSnapshot) -> None:
        """Persist a snapshot. Idempotent on ``snapshot_id``."""
        ...

    def latest(self, source_identifier: str) -> SchemaSnapshot | None:
        """Return the most recent snapshot for ``source_identifier``, or None."""
        ...

    def count(self, source_identifier: str | None = None) -> int:
        """Return the number of stored snapshots, optionally filtered."""
        ...


# ---------------------------------------------------------------------------
# Postgres-backed implementation
# ---------------------------------------------------------------------------


_INSERT_SQL = """
INSERT INTO schema_drift.schema_snapshots
    (snapshot_id, source_kind, source_identifier, captured_at, schema_blob)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (snapshot_id) DO NOTHING
"""

_LATEST_SQL = """
SELECT schema_blob
FROM schema_drift.schema_snapshots
WHERE source_identifier = %s
ORDER BY captured_at DESC, snapshot_id DESC
LIMIT 1
"""

_COUNT_ALL_SQL = "SELECT COUNT(*) AS n FROM schema_drift.schema_snapshots"
_COUNT_BY_SOURCE_SQL = (
    "SELECT COUNT(*) AS n FROM schema_drift.schema_snapshots WHERE source_identifier = %s"
)


class PostgresSnapshotStore:
    """Persist :class:`SchemaSnapshot` to ``schema_drift.schema_snapshots``.

    Parameters
    ----------
    dsn
        libpq connection string. The schema/table must already exist; see
        ``infra/postgres-init.sql`` for the DDL.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    # ----------------------------------------------------------------- public

    def save(self, snapshot: SchemaSnapshot) -> None:
        payload = snapshot.model_dump(mode="json")
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                _INSERT_SQL,
                (
                    snapshot.snapshot_id,
                    snapshot.source_kind.value,
                    snapshot.source_identifier,
                    snapshot.captured_at,
                    Jsonb(payload),
                ),
            )

    def latest(self, source_identifier: str) -> SchemaSnapshot | None:
        with (
            psycopg.connect(self.dsn, row_factory=dict_row) as conn,  # type: ignore[arg-type]
            conn.cursor() as cur,
        ):
            cur.execute(_LATEST_SQL, (source_identifier,))
            row: Any = cur.fetchone()
        if row is None:
            return None
        return SchemaSnapshot.model_validate(row["schema_blob"])

    def count(self, source_identifier: str | None = None) -> int:
        with (
            psycopg.connect(self.dsn, row_factory=dict_row) as conn,  # type: ignore[arg-type]
            conn.cursor() as cur,
        ):
            if source_identifier is None:
                cur.execute(_COUNT_ALL_SQL)
            else:
                cur.execute(_COUNT_BY_SOURCE_SQL, (source_identifier,))
            row: Any = cur.fetchone()
        return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# In-memory implementation (tests + cold-start fallback)
# ---------------------------------------------------------------------------


class InMemorySnapshotStore:
    """RAM-only store. Useful for unit tests and one-shot CLI invocations.

    Snapshots are stored in insertion order; ``latest`` returns the
    most-recently-saved snapshot per ``source_identifier``.
    """

    def __init__(self) -> None:
        # Keyed by source_identifier; value is a list in insertion order.
        self._by_source: dict[str, list[SchemaSnapshot]] = {}

    def save(self, snapshot: SchemaSnapshot) -> None:
        bucket = self._by_source.setdefault(snapshot.source_identifier, [])
        # Idempotent on snapshot_id (mirrors the Postgres ON CONFLICT clause).
        if any(s.snapshot_id == snapshot.snapshot_id for s in bucket):
            return
        bucket.append(snapshot)

    def latest(self, source_identifier: str) -> SchemaSnapshot | None:
        bucket = self._by_source.get(source_identifier)
        if not bucket:
            return None
        # ``captured_at`` is the canonical ordering; ULID tiebreak matches Postgres.
        return max(bucket, key=lambda s: (s.captured_at, s.snapshot_id))

    def count(self, source_identifier: str | None = None) -> int:
        if source_identifier is None:
            return sum(len(b) for b in self._by_source.values())
        return len(self._by_source.get(source_identifier, []))
