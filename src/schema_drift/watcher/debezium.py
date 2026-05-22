"""Debezium adapter — sub-second non-polling mode.

Debezium emits *change events* with embedded key + value Avro schemas.
We turn each value-schema event into a :class:`SchemaSnapshot` table
delta and push it through the same diff/classify pipeline as the
polling watchers — but **driven by message arrival, not a 30-second
timer**.

Why this matters
----------------
The other watchers (Postgres, DuckDB, Snowflake) inherit the
problem in their parent design: polling cadence is the floor on
drift-to-PR latency. If polling is every 30 s, drift-to-PR is
seconds-to-minutes. For shops with Debezium already wired up, we
can do sub-second.

Architecture
------------
Two surfaces:

* :class:`DebeziumWatcher` — backward-compatible ``SourceWatcher``
  implementation. ``snapshot()`` returns the **last-known** snapshot
  assembled from the per-topic schema cache. This lets ``WatcherRunner``
  treat Debezium as just another watcher for the polling loop.
* :class:`DebeziumStreamRunner` — the new event-driven path. Consumes
  one message at a time from an injected consumer protocol, updates the
  per-topic cache, diffs against the prior snapshot, classifies, and
  hands ``DriftEvent``s to a callback. End-to-end latency for a single
  event in CI is <1 ms; against a real Kafka broker it is
  network-RTT + one diff (~10–50 ms).

Both surfaces share the same Avro-decoding helpers, so a downstream
consumer can mix-and-match polling and streaming.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from schema_drift.classifier import Classifier
from schema_drift.models import (
    ColumnSpec,
    DriftEvent,
    SchemaSnapshot,
    SourceKind,
    TableSnapshot,
)
from schema_drift.watcher.base import SourceWatcher

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DebeziumWatcherConfig:
    """Configuration carries only the addressing the user *needs* to
    set; the rest (sasl, ssl, group id, ...) is whatever their Kafka
    consumer ctor takes.
    """

    bootstrap_servers: str
    schema_registry_url: str
    topic_prefix: str  # e.g. "dbserver1.public."
    source_identifier: str
    # Sub-second tuning knobs — see ``DebeziumStreamRunner.run`` for use.
    poll_timeout_seconds: float = 0.05
    max_inflight_events: int = 1024


# ---------------------------------------------------------------------------
# Consumer protocol
# ---------------------------------------------------------------------------


class DebeziumMessage(Protocol):
    """The thinnest message shape we need.

    Designed to be satisfied by ``confluent_kafka.Message`` *and* by a
    plain dataclass in tests. We deliberately do not depend on Confluent's
    types directly.
    """

    def topic(self) -> str: ...  # pragma: no cover

    def value(self) -> dict[str, Any] | bytes | None: ...  # pragma: no cover


class DebeziumConsumer(Protocol):
    """Pull-style consumer protocol.

    Looks like the confluent_kafka consumer but minimal. Tests inject
    a list-backed fake. Production wires in a ``confluent_kafka.Consumer``
    + a ``SchemaRegistryClient`` to decode Avro to dict.
    """

    def poll(self, timeout: float) -> DebeziumMessage | None: ...  # pragma: no cover

    def close(self) -> None: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Watcher (polling-compatible surface)
# ---------------------------------------------------------------------------


class DebeziumWatcher(SourceWatcher):
    """Polling-compatible Debezium watcher.

    Holds the **last-known** per-topic schema in an in-memory cache
    populated either by:

    1. :meth:`prime_from_schemas` (one-shot bootstrap), or
    2. :meth:`ingest_message` (incremental, sub-second), or
    3. :class:`DebeziumStreamRunner` running in a background thread.

    ``snapshot()`` returns whatever the cache currently holds.
    """

    def __init__(
        self,
        config: DebeziumWatcherConfig,
        *,
        consumer: DebeziumConsumer | None = None,
        schema_registry: Any | None = None,
    ) -> None:
        self._config = config
        self._consumer = consumer
        self._registry = schema_registry
        # ``topic -> Avro value-schema dict``. Thread-safe via ``_lock``.
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- public

    @property
    def config(self) -> DebeziumWatcherConfig:
        return self._config

    def prime_from_schemas(self, topic_to_avro: dict[str, dict[str, Any]]) -> None:
        """Bootstrap the cache from a one-shot snapshot.

        Use this when the agent starts up: read each topic's *current*
        latest value schema from the Schema Registry once, hand the
        mapping in here. After that, ``ingest_message`` keeps the cache
        live.
        """
        with self._lock:
            self._cache = dict(topic_to_avro)

    def ingest_message(self, topic: str, avro_value_schema: dict[str, Any]) -> bool:
        """Update the cache for a single topic.

        Returns ``True`` if the cache changed (i.e. the new schema
        differs from what we had), ``False`` if it's a duplicate.
        Sub-second hot path — single dict lookup + assignment under
        a re-entrant lock.
        """
        with self._lock:
            prior = self._cache.get(topic)
            if prior == avro_value_schema:
                return False
            self._cache[topic] = avro_value_schema
            return True

    def snapshot(self) -> SchemaSnapshot:
        """Return a snapshot built from the current cache state.

        If the cache is empty (no consumer feeding it, no
        ``prime_from_schemas`` call), raise ``RuntimeError`` rather
        than silently emitting an empty snapshot — that would look
        like "every table was dropped" to the diff layer.
        """
        with self._lock:
            cache_snapshot = dict(self._cache)
        if not cache_snapshot:
            raise RuntimeError(
                "DebeziumWatcher cache is empty. Call prime_from_schemas() "
                "or run a DebeziumStreamRunner against it before snapshot()."
            )
        return self.from_schema_dict(self._config, cache_snapshot)

    # -------------------------------------------------- legacy constructor

    @classmethod
    def from_schema_dict(
        cls,
        config: DebeziumWatcherConfig,
        topic_to_avro: dict[str, dict[str, Any]],
    ) -> SchemaSnapshot:
        """Build a ``SchemaSnapshot`` deterministically from a topic→Avro map.

        Public + classmethod so downstream test harnesses and the
        :class:`DebeziumStreamRunner` can reuse the conversion without
        instantiating a watcher.
        """
        tables: list[TableSnapshot] = []
        for topic, schema in sorted(topic_to_avro.items()):
            ident = _topic_to_table_identifier(topic, config.topic_prefix)
            cols = _avro_fields_to_columns(schema)
            tables.append(TableSnapshot(table_identifier=ident, columns=cols))
        return SchemaSnapshot(
            source_kind=SourceKind.DEBEZIUM,
            source_identifier=config.source_identifier,
            tables=tuple(tables),
        )


# ---------------------------------------------------------------------------
# Sub-second non-polling runner
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class StreamStats:
    """Counters surfaced by the runner so callers can wire metrics in."""

    messages_consumed: int = 0
    schema_changes_detected: int = 0
    events_emitted: int = 0
    duplicates_skipped: int = 0


class DebeziumStreamRunner:
    """Event-driven driver around a :class:`DebeziumWatcher`.

    Calling :meth:`run` in a thread (or as part of an async loop)
    consumes one message at a time, decodes its value schema, updates
    the watcher cache, and — if the schema changed — diffs against the
    *previous* snapshot, classifies the deltas, and calls
    ``on_events`` with the resulting :class:`DriftEvent` list.

    The whole hot path is single-threaded and lock-free except for
    the cache write; sub-millisecond per message on the in-process
    benchmark.
    """

    def __init__(
        self,
        watcher: DebeziumWatcher,
        consumer: DebeziumConsumer,
        on_events: Callable[[list[DriftEvent], SchemaSnapshot], None],
        *,
        classifier: Classifier | None = None,
        decoder: Callable[[DebeziumMessage], tuple[str, dict[str, Any]] | None] | None = None,
    ) -> None:
        self._watcher = watcher
        self._consumer = consumer
        self._on_events = on_events
        self._classifier = classifier or Classifier()
        self._decoder = decoder or _default_decoder
        self._prev_snapshot: SchemaSnapshot | None = None
        self._stop = threading.Event()
        self._stats = StreamStats()

    # ----------------------------------------------------------------- public

    @property
    def stats(self) -> StreamStats:
        return self._stats

    def stop(self) -> None:
        """Signal the consumer loop to exit at the next iteration boundary."""
        self._stop.set()

    def run(self, *, max_messages: int | None = None) -> StreamStats:
        """Drive the consumer until ``stop()`` is called or budget is hit.

        ``max_messages`` makes the loop terminate after N messages —
        used by tests to keep them deterministic. In production, leave
        it ``None`` and rely on ``stop()``.
        """
        consumed = 0
        while not self._stop.is_set():
            if max_messages is not None and consumed >= max_messages:
                break
            msg = self._consumer.poll(self._watcher.config.poll_timeout_seconds)
            if msg is None:
                continue
            consumed += 1
            self._stats = _bump(self._stats, "messages_consumed", 1)
            decoded = self._decoder(msg)
            if decoded is None:
                continue
            topic, avro_value_schema = decoded
            changed = self._watcher.ingest_message(topic, avro_value_schema)
            if not changed:
                self._stats = _bump(self._stats, "duplicates_skipped", 1)
                continue
            self._stats = _bump(self._stats, "schema_changes_detected", 1)
            curr = self._watcher.snapshot()
            if self._prev_snapshot is not None:
                raw = SourceWatcher.diff(self._prev_snapshot, curr)
                events: list[DriftEvent] = []
                for rc in raw:
                    ev = self._classifier.classify(rc)
                    if ev is not None:
                        events.append(ev)
                if events:
                    self._stats = _bump(self._stats, "events_emitted", len(events))
                    self._on_events(events, curr)
            self._prev_snapshot = curr
        return self._stats


# ---------------------------------------------------------------------------
# Helpers — public-ish
# ---------------------------------------------------------------------------


def iter_messages(
    consumer: DebeziumConsumer, *, max_messages: int | None = None
) -> Iterator[DebeziumMessage]:
    """Generator wrapper around the polling protocol for ad-hoc scripts."""
    n = 0
    while True:
        if max_messages is not None and n >= max_messages:
            return
        msg = consumer.poll(0.05)
        if msg is None:
            continue
        n += 1
        yield msg


def _default_decoder(msg: DebeziumMessage) -> tuple[str, dict[str, Any]] | None:
    """Best-effort decoder.

    Treats the message value as either (a) an already-decoded dict
    (test harnesses + downstream Avro-decoded consumers) or (b) ``None``
    for tombstones we should ignore.
    """
    value = msg.value()
    if value is None:
        return None
    if isinstance(value, dict):
        # Debezium's value envelope sometimes carries the latest schema
        # under ``.schema`` for "schema change events". Honour that path
        # if present; otherwise treat the dict itself as the schema.
        candidate = value.get("schema") if "schema" in value else value
        if isinstance(candidate, dict) and candidate.get("fields"):
            return msg.topic(), candidate
        return None
    # Bytes payloads need a Schema Registry — the caller must supply
    # a custom ``decoder=`` for those. We return None rather than guess.
    return None


# ---------------------------------------------------------------------------
# Avro → ColumnSpec  (unchanged from the stub-era helpers)
# ---------------------------------------------------------------------------


def _topic_to_table_identifier(topic: str, prefix: str) -> str:
    """Debezium topics look like ``dbserver1.public.orders``;
    we want ``public.orders``."""
    if prefix and topic.startswith(prefix):
        return topic[len(prefix) :]
    parts = topic.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else topic


_AVRO_TO_SQL: dict[str, str] = {
    "int": "integer",
    "long": "bigint",
    "float": "real",
    "double": "double precision",
    "string": "text",
    "boolean": "boolean",
    "bytes": "bytea",
}


def _avro_fields_to_columns(schema: dict[str, Any]) -> tuple[ColumnSpec, ...]:
    fields = schema.get("fields") or []
    cols: list[ColumnSpec] = []
    for idx, field_ in enumerate(fields):
        avro_type: Any = field_["type"]
        nullable = False
        if isinstance(avro_type, list):
            non_null = [t for t in avro_type if t != "null"]
            nullable = len(non_null) != len(avro_type)
            avro_type = non_null[0] if non_null else "string"
        if isinstance(avro_type, dict):
            sql_type = _logical_type_to_sql(avro_type)
        else:
            sql_type = _AVRO_TO_SQL.get(str(avro_type), "text")
        cols.append(
            ColumnSpec(
                name=field_["name"],
                data_type=sql_type,
                nullable=nullable,
                ordinal_position=idx,
            )
        )
    return tuple(cols)


def _logical_type_to_sql(avro_type: dict[str, Any]) -> str:
    logical = avro_type.get("logicalType")
    if logical == "decimal":
        precision = avro_type.get("precision", 38)
        scale = avro_type.get("scale", 0)
        return f"numeric({precision},{scale})"
    if logical in {"timestamp-millis", "timestamp-micros"}:
        return "timestamp"
    if logical == "date":
        return "date"
    base = avro_type.get("type", "string")
    return _AVRO_TO_SQL.get(str(base), "text")


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------


def _bump(stats: StreamStats, field_name: str, by: int) -> StreamStats:
    """Return a new ``StreamStats`` with ``field_name`` incremented.

    Frozen-dataclass-friendly accumulator. The Python overhead of
    rebuilding a tiny frozen dataclass is dwarfed by the cost of the
    diff that follows, so keeping ``StreamStats`` frozen is fine.
    """
    kwargs = {
        "messages_consumed": stats.messages_consumed,
        "schema_changes_detected": stats.schema_changes_detected,
        "events_emitted": stats.events_emitted,
        "duplicates_skipped": stats.duplicates_skipped,
    }
    kwargs[field_name] = kwargs[field_name] + by
    return StreamStats(**kwargs)


# Convenience: lets callers feed an iterable of (topic, schema) pairs
# directly into a watcher cache without spinning up a consumer.
def hydrate_cache(
    watcher: DebeziumWatcher,
    pairs: Iterable[tuple[str, dict[str, Any]]],
) -> int:
    """Apply each (topic, schema) pair via ``ingest_message``.

    Returns the count of *new* schemas that actually changed the cache.
    """
    changes = 0
    for topic, schema in pairs:
        if watcher.ingest_message(topic, schema):
            changes += 1
    return changes


__all__ = [
    "DebeziumConsumer",
    "DebeziumMessage",
    "DebeziumStreamRunner",
    "DebeziumWatcher",
    "DebeziumWatcherConfig",
    "StreamStats",
    "hydrate_cache",
    "iter_messages",
]
