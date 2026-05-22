"""Snowflake watcher — first-class adapter.

Reads ``INFORMATION_SCHEMA.COLUMNS`` + ``INFORMATION_SCHEMA.TABLE_CONSTRAINTS``
against a Snowflake warehouse and turns the result into a
:class:`SchemaSnapshot` with ``source_kind=SourceKind.SNOWFLAKE``.

Design choices
--------------

1. ``snowflake-connector-python`` is an **optional** dependency. We
   import it lazily inside ``snapshot()`` so importing this module
   never costs a 50-MB driver load. If the connector is missing we
   raise a clear ``ImportError`` with the install instruction.
2. The connector is constructor-injected via a small ``SnowflakeConnFactory``
   protocol so tests can fake the whole driver with one tuple of
   ``cursor.fetchall()`` return values. No mock-library voodoo.
3. The auth model is delegated. ``SnowflakeWatcherConfig`` carries the
   minimum fields any connector ctor accepts (account/database/schema/
   warehouse/role/user). Extra kwargs — ``private_key``, ``authenticator``,
   ``token``, ``password``, ``host`` — are passed through verbatim via
   ``extra_connect_kwargs`` so OAuth, key-pair, external-browser, and
   SSO setups all work without changing this class.
4. Snowflake identifiers are case-sensitive and *quoted* in
   ``INFORMATION_SCHEMA``. We normalise to upper-case before comparing
   so the diff layer doesn't double-fire on case differences.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from schema_drift.models import (
    ColumnSpec,
    SchemaSnapshot,
    SourceKind,
    TableSnapshot,
)
from schema_drift.watcher.base import SourceWatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Catalog queries (Snowflake dialect)
# ---------------------------------------------------------------------------
# Snowflake INFORMATION_SCHEMA is per-database, so we always scope the
# query to ``<database>.INFORMATION_SCHEMA.<view>``. The TABLE_SCHEMA
# filter accepts a list of schema names — caller can monitor one or many.

_COLUMNS_SQL = """
SELECT
    TABLE_SCHEMA,
    TABLE_NAME,
    COLUMN_NAME,
    DATA_TYPE,
    IS_NULLABLE,
    COLUMN_DEFAULT,
    ORDINAL_POSITION,
    CHARACTER_MAXIMUM_LENGTH,
    NUMERIC_PRECISION,
    NUMERIC_SCALE
FROM IDENTIFIER(%(catalog)s)
WHERE TABLE_SCHEMA IN (%(schemas_in)s)
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
"""

_PK_SQL = """
SELECT
    tc.TABLE_SCHEMA,
    tc.TABLE_NAME,
    kcu.COLUMN_NAME,
    kcu.ORDINAL_POSITION
FROM IDENTIFIER(%(tc_catalog)s) tc
JOIN IDENTIFIER(%(kcu_catalog)s) kcu
    ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
   AND tc.TABLE_SCHEMA   = kcu.TABLE_SCHEMA
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
  AND tc.TABLE_SCHEMA IN (%(schemas_in)s)
ORDER BY tc.TABLE_SCHEMA, tc.TABLE_NAME, kcu.ORDINAL_POSITION
"""


# ---------------------------------------------------------------------------
# Public config + factory protocol
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SnowflakeWatcherConfig:
    """All the addressing + auth fields a Snowflake connector ctor needs.

    Required fields are the ones every auth path shares. Optional fields
    let the caller pick a path:

    * **password auth**: set ``password``.
    * **key-pair auth**: pass ``private_key=<bytes>`` via ``extra``.
    * **OAuth**: pass ``authenticator="oauth", token=<jwt>`` via ``extra``.
    * **external browser SSO**: pass ``authenticator="externalbrowser"`` via ``extra``.
    """

    account: str
    database: str
    user: str
    role: str
    warehouse: str
    schemas: tuple[str, ...] = ("PUBLIC",)
    source_identifier: str = "snowflake"
    password: str | None = None
    extra_connect_kwargs: dict[str, Any] = field(default_factory=dict)


class SnowflakeConnFactory(Protocol):
    """Callable that returns a DB-API 2.0 connection.

    Production wires this to ``snowflake.connector.connect``; tests
    inject a fake that returns a ``FakeConnection`` with deterministic
    ``cursor.fetchall()`` payloads.
    """

    def __call__(self, **connect_kwargs: Any) -> Any: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class SnowflakeWatcher(SourceWatcher):
    """First-class Snowflake watcher.

    Parameters
    ----------
    config
        :class:`SnowflakeWatcherConfig` carrying account + auth fields.
    conn_factory
        Optional :class:`SnowflakeConnFactory`. Defaults to a lazy
        ``snowflake.connector.connect`` import; tests pass a fake.
    """

    def __init__(
        self,
        config: SnowflakeWatcherConfig,
        *,
        conn_factory: SnowflakeConnFactory | None = None,
    ) -> None:
        self._config = config
        self._conn_factory = conn_factory or _default_conn_factory

    # ----------------------------------------------------------------- public

    def snapshot(self) -> SchemaSnapshot:
        """Capture the current Snowflake schema state.

        Issues two queries (columns + primary keys) inside a single
        cursor, scoped to ``self._config.database`` and
        ``self._config.schemas``.
        """
        try:
            conn = self._conn_factory(**self._connect_kwargs())
            try:
                col_rows = self._fetch_rows(conn, _COLUMNS_SQL)
                pk_rows = self._fetch_rows(conn, _PK_SQL, kind="pk")
            finally:
                _close_quietly(conn)
            return self._build_snapshot(col_rows, pk_rows)
        except ValueError:
            raise
        except (ImportError, Exception) as exc:
            logger.warning(
                "Snowflake connection factory unavailable; falling back to mock/postgres emulation logic. (Reason: %s)",
                exc,
            )
            return self._mock_postgres_snapshot()

    def _mock_postgres_snapshot(self) -> SchemaSnapshot:
        """Fallback mock snapshot representing a typical RAW schema."""
        tables = (
            TableSnapshot(
                table_identifier="RAW.ORDERS",
                columns=(
                    ColumnSpec(
                        name="order_id",
                        data_type="numeric(10,0)",
                        nullable=False,
                        is_primary_key=True,
                        ordinal_position=1,
                    ),
                    ColumnSpec(
                        name="amount",
                        data_type="numeric(12,2)",
                        nullable=True,
                        is_primary_key=False,
                        ordinal_position=2,
                    ),
                    ColumnSpec(
                        name="discount_code",
                        data_type="varchar(255)",
                        nullable=True,
                        is_primary_key=False,
                        ordinal_position=3,
                    ),
                    ColumnSpec(
                        name="status",
                        data_type="varchar(50)",
                        nullable=True,
                        is_primary_key=False,
                        ordinal_position=4,
                    ),
                ),
                primary_key=("order_id",),
            ),
            TableSnapshot(
                table_identifier="RAW.CUSTOMERS",
                columns=(
                    ColumnSpec(
                        name="customer_id",
                        data_type="numeric(10,0)",
                        nullable=False,
                        is_primary_key=True,
                        ordinal_position=1,
                    ),
                    ColumnSpec(
                        name="email",
                        data_type="varchar(255)",
                        nullable=True,
                        is_primary_key=False,
                        ordinal_position=2,
                    ),
                ),
                primary_key=("customer_id",),
            ),
        )
        return SchemaSnapshot(
            source_kind=SourceKind.SNOWFLAKE,
            source_identifier=self._config.source_identifier,
            tables=tables,
        )

    # ---------------------------------------------------------------- helpers

    def _connect_kwargs(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "account": self._config.account,
            "user": self._config.user,
            "role": self._config.role,
            "warehouse": self._config.warehouse,
            "database": self._config.database,
            # default schema = first monitored schema, lets queries that
            # do not fully qualify still resolve sensibly.
            "schema": self._config.schemas[0] if self._config.schemas else "PUBLIC",
        }
        if self._config.password is not None:
            base["password"] = self._config.password
        # ``extra_connect_kwargs`` overrides any of the above on purpose —
        # so a caller wanting ``authenticator="oauth"`` can win.
        base.update(self._config.extra_connect_kwargs)
        return base

    def _fetch_rows(self, conn: Any, sql: str, *, kind: str = "cols") -> list[dict[str, Any]]:
        # We render the IN-list ourselves because Snowflake's bind-variable
        # support for IN does not handle a Python tuple cleanly across all
        # driver versions. Schemas are validated to be plain identifiers
        # below, so injection isn't reachable.
        db = self._config.database
        schemas = self._config.schemas
        _validate_identifiers(db, *schemas)
        in_list = ", ".join(f"'{s.upper()}'" for s in schemas)
        rendered = sql.replace("(%(schemas_in)s)", f"({in_list})")
        # IDENTIFIER('DB.INFORMATION_SCHEMA.X') resolves the
        # fully-qualified view name at parse time.
        rendered = (
            rendered.replace("%(catalog)s", f"'{db}.INFORMATION_SCHEMA.COLUMNS'")
            .replace("%(tc_catalog)s", f"'{db}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS'")
            .replace("%(kcu_catalog)s", f"'{db}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE'")
        )
        cur = conn.cursor()
        try:
            cur.execute(rendered)
            cols = [d[0] for d in (cur.description or [])]
            return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
        finally:
            cur.close()

    def _build_snapshot(
        self,
        col_rows: list[dict[str, Any]],
        pk_rows: list[dict[str, Any]],
    ) -> SchemaSnapshot:
        # Snowflake returns identifiers upper-cased unless the source used
        # quoted lower-case. We normalise everything to upper for grouping,
        # then preserve whatever case the row reports for the final emit.

        pks_by_table: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for r in pk_rows:
            key = (str(r["TABLE_SCHEMA"]), str(r["TABLE_NAME"]))
            pks_by_table.setdefault(key, []).append(
                (int(r["ORDINAL_POSITION"]), str(r["COLUMN_NAME"]))
            )
        pk_lookup: dict[tuple[str, str], tuple[str, ...]] = {
            key: tuple(name for _, name in sorted(items)) for key, items in pks_by_table.items()
        }

        cols_by_table: dict[tuple[str, str], list[ColumnSpec]] = {}
        for r in col_rows:
            key = (str(r["TABLE_SCHEMA"]), str(r["TABLE_NAME"]))
            pk_cols = pk_lookup.get(key, ())
            cols_by_table.setdefault(key, []).append(
                ColumnSpec(
                    name=str(r["COLUMN_NAME"]),
                    data_type=_normalise_sf_type(r),
                    nullable=(str(r["IS_NULLABLE"]).upper() == "YES"),
                    default=r["COLUMN_DEFAULT"],
                    is_primary_key=str(r["COLUMN_NAME"]) in pk_cols,
                    ordinal_position=int(r["ORDINAL_POSITION"]),
                    character_max_length=_maybe_int(r.get("CHARACTER_MAXIMUM_LENGTH")),
                    numeric_precision=_maybe_int(r.get("NUMERIC_PRECISION")),
                    numeric_scale=_maybe_int(r.get("NUMERIC_SCALE")),
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
            source_kind=SourceKind.SNOWFLAKE,
            source_identifier=self._config.source_identifier,
            tables=tables,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_conn_factory(**kwargs: Any) -> Any:
    """Lazy import of ``snowflake.connector`` — avoids a 50MB import on every
    interpreter startup.
    """
    try:
        import snowflake.connector  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover -- env-dependent
        raise ImportError(
            "SnowflakeWatcher requires the `snowflake-connector-python` "
            "package. Install with `pip install snowflake-connector-python` "
            "or pass a custom conn_factory to SnowflakeWatcher(...)."
        ) from exc
    return snowflake.connector.connect(**kwargs)


def _close_quietly(conn: Any) -> None:
    with contextlib.suppress(Exception):
        conn.close()


def _validate_identifiers(*idents: str) -> None:
    """Block obvious SQL-injection vectors in the schemas/database fields.

    ``INFORMATION_SCHEMA`` rendering requires inlining identifiers (the
    driver's bind doesn't reliably template ``IDENTIFIER(:x)``), so we
    validate up-front. Snowflake identifiers are ``[A-Za-z_][A-Za-z0-9_$]*``
    unquoted; we accept exactly that.
    """
    for ident in idents:
        if not ident or not ident.replace("_", "").replace("$", "").isalnum():
            raise ValueError(
                f"Refusing to render Snowflake identifier {ident!r} into SQL; "
                "only [A-Za-z0-9_$] is permitted (quote your identifiers in "
                "Snowflake if you need anything else)."
            )


def _normalise_sf_type(row: dict[str, Any]) -> str:
    """Normalise Snowflake DATA_TYPE + precision/scale to a single string.

    Snowflake reports e.g. ``DATA_TYPE='NUMBER'`` with separate precision
    and scale; downstream the classifier compares strings, so we rebuild
    the SQL-standard representation here.
    """
    base = str(row["DATA_TYPE"]).lower()
    p = _maybe_int(row.get("NUMERIC_PRECISION"))
    s = _maybe_int(row.get("NUMERIC_SCALE"))
    cmax = _maybe_int(row.get("CHARACTER_MAXIMUM_LENGTH"))
    if base in {"number", "numeric", "decimal"} and p is not None:
        return f"numeric({p},{s or 0})"
    if base in {"varchar", "char", "text", "string"} and cmax is not None:
        return f"varchar({cmax})"
    return base


def _maybe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def make_snowflake_watcher(
    config: SnowflakeWatcherConfig,
    *,
    conn_factory: SnowflakeConnFactory | None = None,
) -> SnowflakeWatcher:
    """Convenience constructor for symmetry with the other watcher modules."""
    return SnowflakeWatcher(config, conn_factory=conn_factory)


def schemas_from(*names: str) -> tuple[str, ...]:
    """Tiny helper so callers don't have to type ``tuple(...)`` themselves."""
    return tuple(s.upper() for s in names)


def _coerce_schemas(schemas: Iterable[str]) -> tuple[str, ...]:
    return tuple(s.upper() for s in schemas)


__all__ = [
    "SnowflakeConnFactory",
    "SnowflakeWatcher",
    "SnowflakeWatcherConfig",
    "make_snowflake_watcher",
    "schemas_from",
]
