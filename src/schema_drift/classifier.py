"""Pure-rule classifier: ``RawChange`` → ``DriftEvent``.

NO LLM here. Every decision is auditable, unit-testable, and explainable. The
benchmark (Week 5) measures how much accuracy we lose by being deterministic.

Scope
-----
* ``classify(raw)``  → single-RawChange classification. Handles 12 of the 13
  ``ChangeType``s. Returns ``None`` only for diff kinds that fundamentally
  need cross-change correlation (a column rename looks like *removed + added*
  on the wire).
* ``classify_batch(changes)`` → correlates ``removed + added`` pairs into a
  single ``COLUMN_RENAMED`` event when the data types match and the columns
  belong to the same table. This is the only place where the classifier
  is non-stateless.

Type-compatibility rules
------------------------
We use a tiny widening lattice (see ``_WIDEN_GRAPH``). Anything not in the
lattice but with the same family (text→text with a bigger length, numeric
with same precision but bigger scale, …) is examined by ``_compare_types``.
The benchmark validates the rules empirically; revisions live in version
control with a one-line scenario added per rule change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from schema_drift.models import (
    DEFAULT_SEVERITY,
    ChangeType,
    ColumnSpec,
    DriftEvent,
    RawChange,
    SourceKind,
)

# ---------------------------------------------------------------------------
# Type-family helpers
# ---------------------------------------------------------------------------

# Strip parametric suffix (``varchar(50)`` → ``varchar``) for family lookup.
_PARAM_RE = re.compile(r"\s*\(.*\)\s*$")


def _family(data_type: str) -> str:
    return _PARAM_RE.sub("", data_type.strip().lower())


# Directed widening edges. ``(a → b)`` means "a can be losslessly widened
# to b". This is intentionally minimalist; the benchmark will expose gaps.
_WIDEN_GRAPH: dict[str, frozenset[str]] = {
    "smallint": frozenset({"integer", "int", "bigint", "numeric", "decimal"}),
    "integer": frozenset({"bigint", "numeric", "decimal"}),
    "int": frozenset({"bigint", "numeric", "decimal"}),
    "bigint": frozenset({"numeric", "decimal"}),
    "real": frozenset({"double precision", "numeric", "decimal"}),
    "float": frozenset({"double precision", "numeric", "decimal"}),
    "char": frozenset({"varchar", "text"}),
    "varchar": frozenset({"text"}),
    "character varying": frozenset({"text"}),
    "date": frozenset({"timestamp", "timestamp without time zone", "timestamptz"}),
    "time": frozenset({"timestamp", "timestamptz"}),
    "timestamp": frozenset({"timestamptz"}),
    "timestamp without time zone": frozenset({"timestamptz", "timestamp with time zone"}),
}


def _is_widening(before: str, after: str) -> bool:
    b, a = _family(before), _family(after)
    if b == a:
        return False
    return a in _WIDEN_GRAPH.get(b, frozenset())


def _is_narrowing(before: str, after: str) -> bool:
    return _is_widening(after, before)


def _is_same_family_with_bigger_capacity(before: ColumnSpec, after: ColumnSpec) -> bool:
    """text(20)→text(50) and varchar(10)→varchar(255) count as widening."""
    if _family(before.data_type) != _family(after.data_type):
        return False
    b_cap = before.character_max_length
    a_cap = after.character_max_length
    if b_cap is None or a_cap is None:
        return False
    return a_cap > b_cap


def _is_precision_change(before: ColumnSpec, after: ColumnSpec) -> bool:
    """numeric(10,2)→numeric(12,4) — same family, precision/scale moved."""
    if _family(before.data_type) != _family(after.data_type):
        return False
    return (
        before.numeric_precision != after.numeric_precision
        or before.numeric_scale != after.numeric_scale
    )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class Classifier:
    """Map ``RawChange``(s) → typed ``DriftEvent``(s).

    Cheap to construct, idempotent, side-effect free. Two entry points:

    * ``classify(raw)`` for single-change paths (the watcher pipes one diff
      at a time during the demo).
    * ``classify_batch(changes)`` when you want rename detection — it
      correlates removed/added pairs in the same table.
    """

    def __init__(self, source_system: SourceKind = SourceKind.POSTGRES) -> None:
        self.source_system = source_system

    # ------------------------------------------------------------- single API

    def classify(self, raw: RawChange) -> DriftEvent | None:
        """Classify one raw change. Returns ``None`` if the change is benign
        OR if it cannot be classified without a correlated counterpart
        (e.g. a rename appears as both a ``removed`` and an ``added``)."""
        match raw.kind:
            case "added":
                return self._classify_added(raw)
            case "removed":
                return self._classify_removed(raw)
            case "modified":
                return self._classify_modified(raw)
            case "table_added" | "table_removed":
                # Table-level diffs are surfaced as notes, not as standalone
                # events. The orchestrator decides whether to ignore them
                # (new staging table) or fan-out (dropped fact table).
                return None
        return None  # pragma: no cover - exhausted by Literal

    # ------------------------------------------------------------- batch API

    def classify_batch(self, changes: Sequence[RawChange]) -> list[DriftEvent]:
        """Correlate ``removed`` + ``added`` pairs into ``COLUMN_RENAMED``
        events when type & table match; classify the rest individually.

        Heuristic: within one ``table_identifier``, pair each ``removed``
        with the first ``added`` of the same data-type family that doesn't
        already match another removed by name. Surplus singletons fall
        through to the single-change classifier.
        """
        events: list[DriftEvent] = []
        by_table: dict[str, list[RawChange]] = {}
        for ch in changes:
            by_table.setdefault(ch.table_identifier, []).append(ch)

        for table, group in by_table.items():
            removed = [c for c in group if c.kind == "removed"]
            added = [c for c in group if c.kind == "added"]
            paired_added: set[int] = set()

            for r in removed:
                r_col = r.column_before
                if r_col is None:  # pragma: no cover
                    continue
                # Find a same-family added that hasn't been paired yet.
                match_idx = next(
                    (
                        i
                        for i, a in enumerate(added)
                        if i not in paired_added
                        and a.column_after is not None
                        and _family(a.column_after.data_type) == _family(r_col.data_type)
                    ),
                    None,
                )
                if match_idx is None:
                    # No pair — emit a drop.
                    ev = self._classify_removed(r)
                    if ev is not None:
                        events.append(ev)
                    continue
                paired_added.add(match_idx)
                a = added[match_idx]
                events.append(self._make_rename(table, r_col, a.column_after, (r, a)))  # type: ignore[arg-type]

            # Unpaired adds → individual classification.
            for i, a in enumerate(added):
                if i in paired_added:
                    continue
                ev = self._classify_added(a)
                if ev is not None:
                    events.append(ev)

            # Anything else (modified, table_added, table_removed) classified directly.
            for ch in group:
                if ch.kind in ("added", "removed"):
                    continue
                ev = self.classify(ch)
                if ev is not None:
                    events.append(ev)

        return events

    # ---------------------------------------------------------------- helpers

    def _make_event(
        self,
        *,
        source_identifier: str,
        change_type: ChangeType,
        column_before: ColumnSpec | None,
        column_after: ColumnSpec | None,
        raw_changes: Iterable[RawChange],
        notes: tuple[str, ...] = (),
        requires_backfill: bool = False,
    ) -> DriftEvent:
        return DriftEvent(
            source_system=self.source_system,
            source_identifier=source_identifier,
            change_type=change_type,
            severity=DEFAULT_SEVERITY[change_type],
            column_before=column_before,
            column_after=column_after,
            confidence=1.0,
            raw_changes=tuple(raw_changes),
            notes=notes,
            requires_backfill=requires_backfill,
        )

    # --------------------------------------------------------------- per-kind

    def _classify_added(self, raw: RawChange) -> DriftEvent | None:
        col = raw.column_after
        if col is None:  # pragma: no cover -- RawChange validator forbids this
            return None
        if col.nullable:
            change_type = ChangeType.COLUMN_ADDED_NULLABLE
        elif col.default is not None:
            change_type = ChangeType.COLUMN_ADDED_NOT_NULL
        else:
            change_type = ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT
        return self._make_event(
            source_identifier=f"{raw.table_identifier}.{col.name}",
            change_type=change_type,
            column_before=None,
            column_after=col,
            raw_changes=(raw,),
            requires_backfill=change_type
            in (ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT, ChangeType.COLUMN_ADDED_NOT_NULL),
        )

    def _classify_removed(self, raw: RawChange) -> DriftEvent | None:
        col = raw.column_before
        if col is None:  # pragma: no cover
            return None
        return self._make_event(
            source_identifier=f"{raw.table_identifier}.{col.name}",
            change_type=ChangeType.COLUMN_DROPPED,
            column_before=col,
            column_after=None,
            raw_changes=(raw,),
        )

    def _classify_modified(self, raw: RawChange) -> DriftEvent | None:
        b = raw.column_before
        a = raw.column_after
        if b is None or a is None:  # pragma: no cover
            return None

        ident = f"{raw.table_identifier}.{a.name}"

        # 1. PK flag change.
        if b.is_primary_key != a.is_primary_key:
            return self._make_event(
                source_identifier=ident,
                change_type=ChangeType.PK_CHANGED,
                column_before=b,
                column_after=a,
                raw_changes=(raw,),
            )

        # 2. Nullability tightening (True → False).
        if b.nullable and not a.nullable:
            return self._make_event(
                source_identifier=ident,
                change_type=ChangeType.NULLABILITY_TIGHTENED,
                column_before=b,
                column_after=a,
                raw_changes=(raw,),
                requires_backfill=True,
            )

        # 3. Enum value added (and no removal).
        if b.enum_values is not None and a.enum_values is not None:
            before_set = set(b.enum_values)
            after_set = set(a.enum_values)
            if before_set < after_set:  # strict subset → values added, none removed
                return self._make_event(
                    source_identifier=ident,
                    change_type=ChangeType.ENUM_VALUE_ADDED,
                    column_before=b,
                    column_after=a,
                    raw_changes=(raw,),
                )

        # 4. Type change family.
        if _family(b.data_type) != _family(a.data_type):
            if _is_widening(b.data_type, a.data_type):
                ct = ChangeType.TYPE_WIDENED
            elif _is_narrowing(b.data_type, a.data_type):
                ct = ChangeType.TYPE_NARROWED
            else:
                ct = ChangeType.TYPE_INCOMPATIBLE
            return self._make_event(
                source_identifier=ident,
                change_type=ct,
                column_before=b,
                column_after=a,
                raw_changes=(raw,),
                requires_backfill=ct in (ChangeType.TYPE_NARROWED, ChangeType.TYPE_INCOMPATIBLE),
            )

        # 5. Same family, capacity grew → widening.
        if _is_same_family_with_bigger_capacity(b, a):
            return self._make_event(
                source_identifier=ident,
                change_type=ChangeType.TYPE_WIDENED,
                column_before=b,
                column_after=a,
                raw_changes=(raw,),
            )

        # 6. Same family, capacity shrank → narrowing.
        if (
            b.character_max_length is not None
            and a.character_max_length is not None
            and a.character_max_length < b.character_max_length
        ):
            return self._make_event(
                source_identifier=ident,
                change_type=ChangeType.TYPE_NARROWED,
                column_before=b,
                column_after=a,
                raw_changes=(raw,),
                requires_backfill=True,
            )

        # 7. Numeric precision/scale shifts → PRECISION_CHANGED.
        if _is_precision_change(b, a):
            return self._make_event(
                source_identifier=ident,
                change_type=ChangeType.PRECISION_CHANGED,
                column_before=b,
                column_after=a,
                raw_changes=(raw,),
            )

        # 8. Default expression changed (and nothing else).
        if b.default != a.default:
            return self._make_event(
                source_identifier=ident,
                change_type=ChangeType.DEFAULT_CHANGED,
                column_before=b,
                column_after=a,
                raw_changes=(raw,),
            )

        # 9. Truly identical (shouldn't happen — watcher should filter)
        return None  # pragma: no cover

    # ---------------------------------------------------------------- rename

    def _make_rename(
        self,
        table: str,
        before: ColumnSpec,
        after: ColumnSpec,
        raws: tuple[RawChange, RawChange],
    ) -> DriftEvent:
        return self._make_event(
            source_identifier=f"{table}.{after.name}",
            change_type=ChangeType.COLUMN_RENAMED,
            column_before=before,
            column_after=after,
            raw_changes=raws,
            notes=(f"renamed from {before.name!r} → {after.name!r}",),
        )


__all__ = ["Classifier"]
