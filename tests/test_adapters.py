"""Tests for the stretch adapters: debezium stub, snowflake stub, metabase."""

from __future__ import annotations

import httpx
import pytest

from schema_drift.bi.metabase import (
    METABASE_URL_ENV,
    MetabaseAdapter,
    MetabaseConfig,
)
from schema_drift.watcher.debezium import (
    DebeziumWatcher,
    DebeziumWatcherConfig,
)
from schema_drift.watcher.snowflake import SnowflakeWatcher, SnowflakeWatcherConfig

# ---------------------------------------------------------------------------
# Debezium (stub)
# ---------------------------------------------------------------------------


class TestDebezium:
    def test_from_schema_dict_materialises_snapshot(self) -> None:
        cfg = DebeziumWatcherConfig(
            bootstrap_servers="kafka:9092",
            schema_registry_url="http://registry:8081",
            topic_prefix="dbserver1.",
            source_identifier="dbz:dev",
        )
        topic_to_avro = {
            "dbserver1.public.orders": {
                "type": "record",
                "name": "Value",
                "fields": [
                    {"name": "id", "type": "int"},
                    {
                        "name": "amount",
                        "type": {
                            "type": "bytes",
                            "logicalType": "decimal",
                            "precision": 10,
                            "scale": 2,
                        },
                    },
                    {"name": "email", "type": ["null", "string"]},
                ],
            }
        }
        snap = DebeziumWatcher.from_schema_dict(cfg, topic_to_avro)
        assert len(snap.tables) == 1
        orders = snap.tables[0]
        assert orders.table_identifier == "public.orders"
        cols = {c.name: c for c in orders.columns}
        assert cols["id"].data_type == "integer"
        assert cols["id"].nullable is False
        assert cols["amount"].data_type == "numeric(10,2)"
        assert cols["email"].nullable is True
        assert cols["email"].data_type == "text"


# ---------------------------------------------------------------------------
# Snowflake (stub)
# ---------------------------------------------------------------------------


class _FakeSnowflakeCursor:
    """Minimal DB-API 2.0 cursor that returns canned rows by index."""

    def __init__(self, rowsets: list[tuple[list[str], list[tuple]]]) -> None:
        self._rowsets = rowsets
        self._idx = 0
        self.description: list[tuple] | None = None
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        cols, _ = self._rowsets[self._idx]
        self.description = [(c,) for c in cols]

    def fetchall(self) -> list[tuple]:
        _, rows = self._rowsets[self._idx]
        self._idx += 1
        return rows

    def close(self) -> None:
        pass


class _FakeSnowflakeConn:
    def __init__(self, cursor: _FakeSnowflakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _FakeSnowflakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


class TestSnowflake:
    """First-class Snowflake watcher.

    The hot path issues two queries (columns + PKs). We inject a
    fake conn_factory that returns a connection backed by canned
    rowsets and assert the resulting ``SchemaSnapshot``.
    """

    def _config(self) -> SnowflakeWatcherConfig:
        return SnowflakeWatcherConfig(
            account="acme",
            database="ANALYTICS",
            user="bot",
            role="DRIFT_DETECTIVE",
            warehouse="WH_XS",
            schemas=("RAW",),
            password="hunter2",
        )

    def _watcher(self, rowsets: list[tuple[list[str], list[tuple]]]) -> SnowflakeWatcher:
        cursor = _FakeSnowflakeCursor(rowsets)
        conn = _FakeSnowflakeConn(cursor)

        def factory(**kwargs):
            self._last_kwargs = kwargs
            return conn

        return SnowflakeWatcher(self._config(), conn_factory=factory)

    def test_snapshot_round_trips_columns_and_pks(self) -> None:
        col_rows = [
            ("RAW", "ORDERS", "ID", "NUMBER", "NO", None, 1, None, 10, 0),
            ("RAW", "ORDERS", "EMAIL", "VARCHAR", "YES", None, 2, 320, None, None),
            ("RAW", "ORDERS", "AMOUNT", "NUMBER", "YES", None, 3, None, 12, 2),
        ]
        pk_rows = [
            ("RAW", "ORDERS", "ID", 1),
        ]
        rowsets = [
            (
                [
                    "TABLE_SCHEMA",
                    "TABLE_NAME",
                    "COLUMN_NAME",
                    "DATA_TYPE",
                    "IS_NULLABLE",
                    "COLUMN_DEFAULT",
                    "ORDINAL_POSITION",
                    "CHARACTER_MAXIMUM_LENGTH",
                    "NUMERIC_PRECISION",
                    "NUMERIC_SCALE",
                ],
                col_rows,
            ),
            (
                [
                    "TABLE_SCHEMA",
                    "TABLE_NAME",
                    "COLUMN_NAME",
                    "ORDINAL_POSITION",
                ],
                pk_rows,
            ),
        ]
        w = self._watcher(rowsets)
        snap = w.snapshot()
        assert snap.source_kind.value == "snowflake"
        assert len(snap.tables) == 1
        orders = snap.tables[0]
        assert orders.table_identifier == "RAW.ORDERS"
        cols = {c.name: c for c in orders.columns}
        assert cols["ID"].is_primary_key is True
        assert cols["ID"].nullable is False
        assert cols["ID"].data_type == "numeric(10,0)"
        assert cols["EMAIL"].data_type == "varchar(320)"
        assert cols["EMAIL"].nullable is True
        assert cols["AMOUNT"].data_type == "numeric(12,2)"
        assert orders.primary_key == ("ID",)

    def test_connect_kwargs_pass_through_extras(self) -> None:
        cfg = SnowflakeWatcherConfig(
            account="acme",
            database="ANALYTICS",
            user="bot",
            role="DRIFT_DETECTIVE",
            warehouse="WH_XS",
            schemas=("RAW",),
            extra_connect_kwargs={"authenticator": "externalbrowser"},
        )
        captured: dict = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _FakeSnowflakeConn(
                _FakeSnowflakeCursor(
                    [
                        (["TABLE_SCHEMA"], []),
                        (["TABLE_SCHEMA"], []),
                    ]
                )
            )

        w = SnowflakeWatcher(cfg, conn_factory=factory)
        w.snapshot()
        assert captured["authenticator"] == "externalbrowser"
        assert captured["account"] == "acme"
        assert captured["database"] == "ANALYTICS"

    def test_identifier_validation_rejects_injection(self) -> None:
        cfg = SnowflakeWatcherConfig(
            account="acme",
            database="ANALYTICS; DROP TABLE USERS--",
            user="bot",
            role="r",
            warehouse="WH",
            schemas=("RAW",),
        )
        w = SnowflakeWatcher(
            cfg,
            conn_factory=lambda **kw: _FakeSnowflakeConn(
                _FakeSnowflakeCursor([(["x"], []), (["x"], [])])
            ),
        )
        with pytest.raises(ValueError, match="Refusing to render"):
            w.snapshot()


# ---------------------------------------------------------------------------
# Metabase
# ---------------------------------------------------------------------------


class _RecordingTransport(httpx.MockTransport):
    def __init__(self, payload: list[dict]) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=payload)

        super().__init__(handler)


class TestMetabase:
    def test_disabled_when_url_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(METABASE_URL_ENV, raising=False)
        a = MetabaseAdapter()
        assert a.enabled is False
        assert a.dashboards_for("fct_orders") == ()
        assert a.fetch_dashboards() == {}

    def test_groups_cards_by_referenced_model(self) -> None:
        cards = [
            {
                "id": 11,
                "name": "Exec Revenue",
                "dataset_query": {
                    "native": {"query": "SELECT * FROM analytics.fct_orders WHERE 1=1"},
                },
            },
            {
                "id": 12,
                "name": "Daily backfill check",
                "dataset_query": {
                    "native": {
                        "query": "SELECT count(*) FROM analytics.fct_orders JOIN analytics.mart_revenue_daily USING(d)"
                    }
                },
            },
            {
                "id": 13,
                "name": "Card without SQL",
                "dataset_query": {"query": {"source-table": 99}},
            },
        ]
        tr = _RecordingTransport(cards)
        client = httpx.Client(transport=tr, base_url="http://mb")
        a = MetabaseAdapter(
            config=MetabaseConfig(url="http://mb", api_key="abc"),
            client=client,
        )
        mapping = a.fetch_dashboards()
        assert set(mapping.keys()) == {"fct_orders", "mart_revenue_daily"}
        # 2 cards reference fct_orders.
        assert len(mapping["fct_orders"]) == 2
        names = {ref.name for ref in mapping["fct_orders"]}
        assert names == {"Exec Revenue", "Daily backfill check"}
        # Tiering: "exec" in name → critical.
        exec_ref = next(r for r in mapping["fct_orders"] if r.name == "Exec Revenue")
        assert exec_ref.tier == "critical"
        # API key header was sent.
        assert tr.requests[0].headers.get("x-api-key") == "abc"

    def test_dashboards_for_returns_empty_tuple_on_unknown_model(self) -> None:
        tr = _RecordingTransport([])
        client = httpx.Client(transport=tr, base_url="http://mb")
        a = MetabaseAdapter(config=MetabaseConfig(url="http://mb"), client=client)
        assert a.dashboards_for("unknown_model") == ()

    def test_refresh_clears_cache(self) -> None:
        tr = _RecordingTransport(
            [
                {
                    "id": 1,
                    "name": "C",
                    "dataset_query": {"native": {"query": "SELECT 1 FROM x.y"}},
                }
            ]
        )
        client = httpx.Client(transport=tr, base_url="http://mb")
        a = MetabaseAdapter(config=MetabaseConfig(url="http://mb"), client=client)
        a.fetch_dashboards()
        assert a._loaded is True
        a.refresh()
        assert a._loaded is False
        assert a._model_to_cards == {}

    def test_extract_returns_dot_qualified_table_short_name(self) -> None:
        # White-box: the grep should pull "y" out of "x.y".
        from schema_drift.bi.metabase import _grep_from_tables

        assert _grep_from_tables("SELECT * FROM schema.table_a JOIN schema.table_b ON 1=1") == [
            "table_a",
            "table_b",
        ]
        # No bogus matches in subqueries.
        assert _grep_from_tables("SELECT (SELECT 1) FROM x.t1") == ["t1"]


# ---------------------------------------------------------------------------
# Debezium — sub-second non-polling mode
# ---------------------------------------------------------------------------


class _FakeDbzMessage:
    def __init__(self, topic: str, value: dict | None) -> None:
        self._t = topic
        self._v = value

    def topic(self) -> str:
        return self._t

    def value(self) -> dict | None:
        return self._v


class _FakeDbzConsumer:
    """List-backed consumer. Drains one message per ``poll`` call."""

    def __init__(self, msgs: list[_FakeDbzMessage]) -> None:
        self._msgs = list(msgs)
        self.closed = False

    def poll(self, timeout: float) -> _FakeDbzMessage | None:
        if not self._msgs:
            return None
        return self._msgs.pop(0)

    def close(self) -> None:
        self.closed = True


class TestDebeziumStreaming:
    """Sub-second non-polling mode.

    Drives a :class:`DebeziumStreamRunner` against a list-backed
    consumer and asserts events fire when the value schema changes
    between two messages on the same topic.
    """

    def _orders_schema(self, *, with_email: bool) -> dict:
        fields = [
            {"name": "id", "type": "int"},
            {"name": "amount", "type": ["null", "long"]},
        ]
        if with_email:
            fields.append({"name": "email", "type": ["null", "string"]})
        return {"type": "record", "name": "Value", "fields": fields}

    def test_stream_runner_fires_drift_on_added_column(self) -> None:
        from schema_drift.watcher.debezium import (
            DebeziumStreamRunner,
            DebeziumWatcher,
            DebeziumWatcherConfig,
        )

        cfg = DebeziumWatcherConfig(
            bootstrap_servers="kafka:9092",
            schema_registry_url="http://registry:8081",
            topic_prefix="dbserver1.",
            source_identifier="dbz:dev",
        )
        watcher = DebeziumWatcher(cfg)
        msgs = [
            _FakeDbzMessage("dbserver1.public.orders", self._orders_schema(with_email=False)),
            _FakeDbzMessage("dbserver1.public.orders", self._orders_schema(with_email=True)),
        ]
        consumer = _FakeDbzConsumer(msgs)

        captured: list = []

        def on_events(events, snap):
            captured.append((events, snap))

        runner = DebeziumStreamRunner(watcher, consumer, on_events)
        stats = runner.run(max_messages=2)

        assert stats.messages_consumed == 2
        assert stats.schema_changes_detected == 2  # first prime, second add
        assert stats.events_emitted >= 1, "second message added an email column"
        # First batch only had the prime; second was the diff.
        assert len(captured) == 1
        events, snap = captured[0]
        assert any(e.change_type.value.startswith("column_added") for e in events)
        assert snap.source_kind.value == "debezium"

    def test_stream_runner_skips_duplicates(self) -> None:
        from schema_drift.watcher.debezium import (
            DebeziumStreamRunner,
            DebeziumWatcher,
            DebeziumWatcherConfig,
        )

        cfg = DebeziumWatcherConfig(
            bootstrap_servers="kafka:9092",
            schema_registry_url="http://registry:8081",
            topic_prefix="dbserver1.",
            source_identifier="dbz:dev",
        )
        watcher = DebeziumWatcher(cfg)
        same = self._orders_schema(with_email=False)
        msgs = [
            _FakeDbzMessage("dbserver1.public.orders", same),
            _FakeDbzMessage("dbserver1.public.orders", same),
            _FakeDbzMessage("dbserver1.public.orders", same),
        ]
        runner = DebeziumStreamRunner(watcher, _FakeDbzConsumer(msgs), on_events=lambda *a: None)
        stats = runner.run(max_messages=3)
        assert stats.schema_changes_detected == 1
        assert stats.duplicates_skipped == 2

    def test_polling_snapshot_uses_cache(self) -> None:
        from schema_drift.watcher.debezium import (
            DebeziumWatcher,
            DebeziumWatcherConfig,
        )

        cfg = DebeziumWatcherConfig(
            bootstrap_servers="kafka:9092",
            schema_registry_url="http://registry:8081",
            topic_prefix="dbserver1.",
            source_identifier="dbz:dev",
        )
        watcher = DebeziumWatcher(cfg)
        watcher.prime_from_schemas(
            {"dbserver1.public.orders": self._orders_schema(with_email=True)}
        )
        snap = watcher.snapshot()
        assert len(snap.tables) == 1
        assert {c.name for c in snap.tables[0].columns} == {"id", "amount", "email"}

    def test_snapshot_raises_when_cache_empty(self) -> None:
        from schema_drift.watcher.debezium import (
            DebeziumWatcher,
            DebeziumWatcherConfig,
        )

        cfg = DebeziumWatcherConfig(
            bootstrap_servers="kafka:9092",
            schema_registry_url="http://registry:8081",
            topic_prefix="dbserver1.",
            source_identifier="dbz:dev",
        )
        watcher = DebeziumWatcher(cfg)
        with pytest.raises(RuntimeError, match="cache is empty"):
            watcher.snapshot()


# ---------------------------------------------------------------------------
# Snowflake convenience helpers
# ---------------------------------------------------------------------------


class TestSnowflakeHelpers:
    def test_schemas_from_uppers(self) -> None:
        from schema_drift.watcher.snowflake import schemas_from

        assert schemas_from("raw", "Staging") == ("RAW", "STAGING")
