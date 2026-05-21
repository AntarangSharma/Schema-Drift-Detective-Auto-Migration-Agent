"""Single-cycle watcher orchestration.

A ``WatcherRunner`` glues four collaborators into one pass:

    watcher.snapshot() → store.latest() → diff → classifier → store.save()

This module deliberately owns no IO of its own — every side-effect is
delegated to an injected collaborator so the runner is trivial to unit-test
with fakes.

Semantics of the first call
---------------------------
The first invocation against a given ``source_identifier`` has no previous
snapshot to diff against. We treat that as a **baseline capture**, not a
drift: persist the snapshot, return an empty events list, and surface
``RunResult.is_baseline = True`` so the CLI / daemon can log it accordingly
without conflating it with the "no events detected" steady state.
"""

from __future__ import annotations

from dataclasses import dataclass

from schema_drift.classifier import Classifier
from schema_drift.models import DriftEvent, SchemaSnapshot
from schema_drift.storage import SnapshotStore
from schema_drift.watcher.base import SourceWatcher


@dataclass(slots=True, frozen=True)
class RunResult:
    """Outcome of a single ``WatcherRunner.run_once`` call.

    ``is_baseline`` is True iff this was the first observed snapshot for this
    source (no previous snapshot was on file). In that case ``events`` is
    empty by construction.
    """

    snapshot: SchemaSnapshot
    events: list[DriftEvent]
    is_baseline: bool

    @property
    def event_count(self) -> int:
        return len(self.events)


class WatcherRunner:
    """Run a single ``snapshot → diff → classify → persist`` cycle.

    Parameters
    ----------
    watcher
        Any :class:`SourceWatcher` (Postgres in prod, ``FakeWatcher`` in tests).
    store
        Any object implementing :class:`SnapshotStore`. The runner reads the
        previous snapshot from it and writes the new one back.
    classifier
        Optional :class:`Classifier`. Defaults to a fresh ``Classifier()`` so
        callers don't have to wire one explicitly.
    """

    def __init__(
        self,
        watcher: SourceWatcher,
        store: SnapshotStore,
        classifier: Classifier | None = None,
    ) -> None:
        self.watcher = watcher
        self.store = store
        self.classifier = classifier or Classifier()

    # ----------------------------------------------------------------- public

    def run_once(self) -> RunResult:
        """Capture, diff, classify, persist. Returns the result of this cycle.

        The new snapshot is always persisted — even when no events fire — so
        the next cycle has a valid baseline. Persistence is idempotent on
        ``snapshot_id`` (see the store contract).
        """
        current = self.watcher.snapshot()
        previous = self.store.latest(current.source_identifier)

        # Always persist *before* returning so the very next call has a
        # baseline. Doing this first also means a downstream classifier crash
        # doesn't lose the snapshot.
        self.store.save(current)

        if previous is None:
            return RunResult(snapshot=current, events=[], is_baseline=True)

        raw_changes = SourceWatcher.diff(previous, current)
        events: list[DriftEvent] = []
        for rc in raw_changes:
            ev = self.classifier.classify(rc)
            if ev is not None:
                events.append(ev)

        return RunResult(snapshot=current, events=events, is_baseline=False)


__all__ = ["RunResult", "WatcherRunner"]
