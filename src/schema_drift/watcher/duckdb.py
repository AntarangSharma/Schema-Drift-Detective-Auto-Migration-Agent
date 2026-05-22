"""DuckDB watcher.

A minimal smoke watcher: connect to a DuckDB file (or in-memory DB),
read ``information_schema.columns``, materialise a ``SchemaSnapshot``.
Useful for:

* Local demos (no Postgres required).
* The end-to-end test (``tests/test_duckdb_e2e.py``).
* Catching drift in seed/staging duckdb files that some shops use
  as an analytics layer.

Why not Postgres-by-default
---------------------------
DuckDB starts in <50 ms with zero dependencies; Postgres needs a
container. For "does the agent work?" the answer should be "run one
``pytest``", not "spin up docker compose".

Caveats
-------
* DuckDB doesn't model enum columns the same way Postgres does;
  we don't try to harvest ``enum_values``.
* ``character_max_length`` reads from ``information_schema``, which
  DuckDB returns as NULL for un-sized types. We pass that through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from schema_drift.models import ColumnSpec, SchemaSnapshot, SourceKind, TableSnapshot
from schema_drift.watcher.base import SourceWatcher

if TYPE_CHECKING:  # pragma: no cover
    pass

try:
    import duckdb as _duckdb  # type: ignore[import-not-found]

    _DUCKDB_AVAILABLE = True
except ImportError:  # pragma: no cover -- optional dep
    _duckdb = None  # type: ignore[assignment]
    _DUCKDB_AVAILABLE = False


@dataclass(slots=True)
class DuckDBWatcherConfig:
    database: str = ":memory:"  # file path or ":memory:"
    schema: str = "main"
    source_identifier: str | None = None  # defaults to f"duckdb:{database}"


class DuckDBWatcher(SourceWatcher):
    """Snapshot a DuckDB schema via ``information_schema``."""

    def __init__(
        self,
        config: DuckDBWatcherConfig | None = None,
        *,
        connection: Any = None,
    ) -> None:
        if not _DUCKDB_AVAILABLE and connection is None:
            raise RuntimeError(
                "duckdb is not installed. Install with `pip install duckdb` "
                "to use DuckDBWatcher, or pass a pre-built connection."
            )
        self._config = config or DuckDBWatcherConfig()
        if connection is None:
            assert _duckdb is not None
            self._conn = _duckdb.connect(self._config.database)
        else:
            self._conn = connection

    # ------------------------------------------------------------------ #
    # SourceWatcher impl                                                  #
    # ------------------------------------------------------------------ #

    def snapshot(self) -> SchemaSnapshot:
        rows = self._query_columns()
        tables: dict[str, list[ColumnSpec]] = {}
        pk_map: dict[str, list[str]] = {}

        for row in rows:
            (
                table_schema,
                table_name,
                column_name,
                data_type,
                is_nullable,
                column_default,
                ordinal_position,
                char_max_length,
                numeric_precision,
                numeric_scale,
            ) = row
            ident = f"{table_schema}.{table_name}"
            col = ColumnSpec(
                name=column_name,
                data_type=str(data_type).lower(),
                nullable=(str(is_nullable).upper() == "YES"),
                default=str(column_default) if column_default is not None else None,
                ordinal_position=int(ordinal_position),
                character_max_length=int(char_max_length) if char_max_length is not None else None,
                numeric_precision=int(numeric_precision) if numeric_precision is not None else None,
                numeric_scale=int(numeric_scale) if numeric_scale is not None else None,
            )
            tables.setdefault(ident, []).append(col)

        # DuckDB stores PK info in duckdb_constraints (>=0.9). Best-effort.
        try:
            pk_rows = self._conn.execute(
                "SELECT table_name, constraint_column_names "
                "FROM duckdb_constraints() "
                "WHERE constraint_type = 'PRIMARY KEY'"
            ).fetchall()
            for tname, cols in pk_rows:
                ident = f"{self._config.schema}.{tname}"
                pk_map[ident] = list(cols) if cols else []
        except Exception:  # pragma: no cover -- older duckdb without duckdb_constraints
            pass

        snapshots: list[TableSnapshot] = []
        for ident, cols in sorted(tables.items()):
            cols.sort(key=lambda c: c.ordinal_position)
            for col in cols:
                if col.name in pk_map.get(ident, []):
                    # Rebuild with is_primary_key=True (Pydantic models are frozen)
                    idx = cols.index(col)
                    cols[idx] = col.model_copy(update={"is_primary_key": True})
            snapshots.append(
                TableSnapshot(
                    table_identifier=ident,
                    columns=tuple(cols),
                    primary_key=tuple(pk_map.get(ident, [])),
                )
            )

        return SchemaSnapshot(
            source_kind=SourceKind.DUCKDB,
            source_identifier=self._config.source_identifier or f"duckdb:{self._config.database}",
            tables=tuple(snapshots),
        )

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _query_columns(self) -> list[tuple[Any, ...]]:
        sql = """
            SELECT table_schema,
                   table_name,
                   column_name,
                   data_type,
                   is_nullable,
                   column_default,
                   ordinal_position,
                   character_maximum_length,
                   numeric_precision,
                   numeric_scale
            FROM information_schema.columns
            WHERE table_schema = ?
            ORDER BY table_name, ordinal_position
        """
        return self._conn.execute(sql, [self._config.schema]).fetchall()


__all__ = ["DuckDBWatcher", "DuckDBWatcherConfig"]
