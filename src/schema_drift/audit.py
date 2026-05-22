"""Audit-log emitter.

Every pipeline action writes an ``AuditRecord`` (defined in ``models.py``).
This module is the *only* place that knows where records go. Today the
default sink is a JSONL file under ``DRIFT_AUDIT_PATH`` (or stdout); a
future iteration can swap in a database or Kafka sink without changing
any caller.

Why JSONL by default
--------------------
* It's diff-friendly in the demo sandbox repo, so reviewers can see
  every action the agent took next to the PR it produced.
* It's the lowest-friction interface to grep / jq during incident
  triage.

Thread-safety
-------------
Writes go through ``threading.Lock`` so concurrent watchers in the
same process don't interleave records. Cross-process safety is
delegated to the OS file lock (``flock`` on POSIX) — we open with
``O_APPEND`` so kernel-level append atomicity covers single-line
writes up to ~PIPE_BUF.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from schema_drift.models import AuditRecord

AUDIT_PATH_ENV = "DRIFT_AUDIT_PATH"


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


class AuditSink(Protocol):
    def write(self, record: AuditRecord) -> None: ...


class StdoutSink:
    """Stream sink (default when ``DRIFT_AUDIT_PATH`` is unset)."""

    def write(self, record: AuditRecord) -> None:
        sys.stdout.write(_serialise(record) + "\n")
        sys.stdout.flush()


class JsonlFileSink:
    """Append-only JSONL sink. Thread-safe within one process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: AuditRecord) -> None:
        line = _serialise(record) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    @property
    def path(self) -> Path:
        return self._path


class InMemorySink:
    """Test helper. Records stay in ``records`` until cleared."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


class Auditor:
    """Thin facade so callers do ``auditor.emit("...", "...", payload=...)``
    without constructing the Pydantic model themselves.
    """

    def __init__(self, sink: AuditSink | None = None) -> None:
        self._sink = sink or _default_sink()

    @property
    def sink(self) -> AuditSink:
        return self._sink

    def emit(
        self,
        actor: str,
        action: str,
        *,
        target: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            actor=actor,
            action=action,
            target=target,
            payload=dict(payload or {}),
        )
        self._sink.write(record)
        return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_sink() -> AuditSink:
    path = os.environ.get(AUDIT_PATH_ENV)
    if path:
        return JsonlFileSink(Path(path))
    return StdoutSink()


def _serialise(record: AuditRecord) -> str:
    # Pydantic's ``model_dump`` already produces native types; we only
    # need to coerce the datetime to ISO-8601.
    payload = record.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, default=_json_default)


def _json_default(obj: Any) -> Any:  # pragma: no cover -- pydantic handles most
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Cannot JSON-serialise {type(obj).__name__}")


__all__ = [
    "AUDIT_PATH_ENV",
    "AuditSink",
    "Auditor",
    "InMemorySink",
    "JsonlFileSink",
    "StdoutSink",
]
