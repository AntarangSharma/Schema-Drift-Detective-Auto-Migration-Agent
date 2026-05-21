"""Pure-rule classifier: ``RawChange`` → ``DriftEvent``.

NO LLM here. Every decision is auditable and unit-testable. The benchmark
(Week 5) will measure exactly how much accuracy is lost by being deterministic;
if the loss is small, rules win on cost, latency, and explainability.

Day-3 scope: ``COLUMN_ADDED_NULLABLE`` only. Days 4-7 expand to all 13.
"""

from __future__ import annotations

from schema_drift.models import (
    DEFAULT_SEVERITY,
    ChangeType,
    DriftEvent,
    RawChange,
    SourceKind,
)


class Classifier:
    """Map a single ``RawChange`` to a typed ``DriftEvent`` (or ``None``).

    The classifier is intentionally stateless and cheap to construct.

    Parameters
    ----------
    source_system
        Used to populate ``DriftEvent.source_system``. Defaults to Postgres
        for the Day-3 demo.
    """

    def __init__(self, source_system: SourceKind = SourceKind.POSTGRES) -> None:
        self.source_system = source_system

    def classify(self, raw: RawChange) -> DriftEvent | None:
        """Classify one raw change. ``None`` means the change is benign / unrecognised."""
        match raw.kind:
            case "added":
                return self._classify_added(raw)
            case _:
                # Other kinds wired up in Week 1 Days 4-7. Returning None
                # here is fine; the demo only exercises the "added" path.
                return None

    # ------------------------------------------------------------------ rules

    def _classify_added(self, raw: RawChange) -> DriftEvent | None:
        col = raw.column_after
        if col is None:  # pragma: no cover -- RawChange validator already forbids this
            return None

        if col.nullable:
            change_type = ChangeType.COLUMN_ADDED_NULLABLE
        elif col.default is not None:
            change_type = ChangeType.COLUMN_ADDED_NOT_NULL
        else:
            change_type = ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT

        return DriftEvent(
            source_system=self.source_system,
            source_identifier=f"{raw.table_identifier}.{col.name}",
            change_type=change_type,
            severity=DEFAULT_SEVERITY[change_type],
            column_before=None,
            column_after=col,
            confidence=1.0,
            raw_changes=(raw,),
        )
