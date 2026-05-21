"""Postgres watcher — polls ``information_schema`` + ``pg_*`` catalogs.

Why polling and not Debezium? See ``docs/02_revised_plan.md`` (decision #1):
polling is cross-warehouse-portable, requires zero JVM/Kafka infra, and
schema-change latency in seconds is fine for this problem. A Debezium
adapter conforming to the same ``SourceWatcher`` interface is a one-file
swap planned for Week 7.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.rows import dict_row

from schema_drift.models import ColumnSpec, SchemaSnapshot, SourceKind, TableSnapshot
from schema_drift.watcher.base import SourceWatcher

# Each catalog query returns dicts; we type them as ``dict[str, Any]`` so we
# don't drag psycopg's private generic aliases into our own public surface.
_Row = dict[str, Any]


# ---------------------------------------------------------------------------
# Catalog queries
# ---------------------------------------------------------------------------
# Kept as module-level constants so they're easy to spot in diffs and
# possible to override in tests. Both queries take a single parameter:
# a list of schema names to inspect.

_COLUMNS_SQL = """
SELECT
    table_schema,
    table_name,
    column_name,
    data_type,
    udt_name,
    is_nullable,
    column_default,
    ordinal_position,
    character_maximum_length,
    numeric_precision,
    numeric_scale
FROM information_schema.columns
WHERE table_schema = ANY(%s)
ORDER BY table_schema, table_name, ordinal_position
"""

_PK_SQL = """
SELECT
    tc.table_schema,
    tc.table_name,
    kcu.column_name,
    kcu.ordinal_position
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema  = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
  AND tc.table_schema = ANY(%s)
ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position
"""


class PostgresWatcher(SourceWatcher):
    """Snapshots Postgres schemas via ``information_schema``.

    Parameters
    ----------
    dsn
        libpq connection string, e.g. ``"postgresql://drift:drift@localhost:5432/drift"``.
    schemas
        Schema names to monitor. Defaults to ``("source_raw",)``.
    source_identifier
        Human-readable label used in events and audit logs. Defaults to
        ``"postgres"``.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schemas: Iterable[str] = ("source_raw",),
        source_identifier: str = "postgres",
    ) -> None:
        self.dsn = dsn
        self.schemas = list(schemas)
        self.source_identifier = source_identifier

    # ------------------------------------------------------------------ public

    def snapshot(self) -> SchemaSnapshot:
        # ``row_factory=dict_row`` makes every fetch return dicts. psycopg's
        # type stubs default the connection to ``TupleRow``; we therefore
        # treat the connection opaquely (``Any``) inside helpers — the SQL
        # contract is enforced by the query column lists, not the type system.
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:  # type: ignore[arg-type]
            col_rows = self._fetch_columns(conn)
            pk_rows = self._fetch_primary_keys(conn)

        return self._build_snapshot(col_rows, pk_rows)

    # ----------------------------------------------------------------- helpers

    def _fetch_columns(self, conn: Any) -> list[_Row]:
        with conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL, (self.schemas,))
            return cur.fetchall()

    def _fetch_primary_keys(self, conn: Any) -> list[_Row]:
        with conn.cursor() as cur:
            cur.execute(_PK_SQL, (self.schemas,))
            return cur.fetchall()

    def _build_snapshot(
        self,
        col_rows: list[_Row],
        pk_rows: list[_Row],
    ) -> SchemaSnapshot:
        # Group PKs by (schema, table).
        pks_by_table: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for r in pk_rows:
            key = (r["table_schema"], r["table_name"])
            pks_by_table.setdefault(key, []).append((r["ordinal_position"], r["column_name"]))

        pk_lookup: dict[tuple[str, str], tuple[str, ...]] = {
            key: tuple(name for _, name in sorted(items)) for key, items in pks_by_table.items()
        }

        # Group columns by (schema, table).
        cols_by_table: dict[tuple[str, str], list[ColumnSpec]] = {}
        for r in col_rows:
            key = (r["table_schema"], r["table_name"])
            pk_cols = pk_lookup.get(key, ())
            cols_by_table.setdefault(key, []).append(
                ColumnSpec(
                    name=r["column_name"],
                    data_type=r["udt_name"] or r["data_type"],
                    nullable=(r["is_nullable"] == "YES"),
                    default=r["column_default"],
                    is_primary_key=r["column_name"] in pk_cols,
                    ordinal_position=r["ordinal_position"],
                    character_max_length=r["character_maximum_length"],
                    numeric_precision=r["numeric_precision"],
                    numeric_scale=r["numeric_scale"],
                )
            )

        tables = tuple(
            TableSnapshot(
                table_identifier=f"{schema}.{table}",
                columns=tuple(cols),
                primary_key=pk_lookup.get((schema, table), ()),
            )
            for (schema, table), cols in sorted(cols_by_table.items())
        )

        return SchemaSnapshot(
            source_kind=SourceKind.POSTGRES,
            source_identifier=self.source_identifier,
            tables=tables,
        )
