"""Tests for the rule-based classifier — full 13-ChangeType coverage.

The grouping mirrors ``ChangeType`` so a failure points at the rule under
test without grep. Each section has 1 happy + 1 edge case unless the rule
has no meaningful edge (e.g. table-level no-ops).
"""

from __future__ import annotations

from schema_drift.classifier import Classifier
from schema_drift.models import (
    ChangeType,
    ColumnSpec,
    RawChange,
    Severity,
)


def _col(name: str = "c", **over) -> ColumnSpec:
    base = {"name": name, "data_type": "text", "nullable": True, "ordinal_position": 1}
    base.update(over)
    return ColumnSpec(**base)  # type: ignore[arg-type]


def _raw(kind, table="s.t", **over) -> RawChange:
    base: dict = {"table_identifier": table, "kind": kind}
    base.update(over)
    return RawChange(**base)


# ---------------------------------------------------------------------------
# COLUMN_ADDED_*  (3 variants)
# ---------------------------------------------------------------------------


class TestColumnAdded:
    def test_nullable_add(self):
        ev = Classifier().classify(_raw("added", column_after=_col(name="discount_code")))
        assert ev
        assert ev.change_type is ChangeType.COLUMN_ADDED_NULLABLE
        assert ev.severity is Severity.LOW
        assert ev.requires_backfill is False

    def test_not_null_with_default(self):
        ev = Classifier().classify(_raw("added", column_after=_col(nullable=False, default="'x'")))
        assert ev
        assert ev.change_type is ChangeType.COLUMN_ADDED_NOT_NULL
        assert ev.requires_backfill is True  # NOT NULL still wants a backfill check

    def test_not_null_no_default_bumps_severity(self):
        ev = Classifier().classify(_raw("added", column_after=_col(nullable=False)))
        assert ev
        assert ev.change_type is ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT
        assert ev.severity is Severity.MEDIUM
        assert ev.requires_backfill is True


# ---------------------------------------------------------------------------
# COLUMN_DROPPED
# ---------------------------------------------------------------------------


class TestColumnDropped:
    def test_drop_emits_high_severity(self):
        ev = Classifier().classify(_raw("removed", column_before=_col(name="email")))
        assert ev
        assert ev.change_type is ChangeType.COLUMN_DROPPED
        assert ev.severity is Severity.HIGH
        # Destructive ⇒ never auto-mergeable.
        assert ev.auto_mergeable is False

    def test_drop_preserves_before_column(self):
        ev = Classifier().classify(_raw("removed", column_before=_col(name="ssn")))
        assert ev
        assert ev.column_before is not None
        assert ev.column_after is None


# ---------------------------------------------------------------------------
# TYPE_WIDENED / NARROWED / INCOMPATIBLE
# ---------------------------------------------------------------------------


class TestTypeChanges:
    def test_widened_int_to_bigint(self):
        b = _col(data_type="integer", nullable=False)
        a = _col(data_type="bigint", nullable=False)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.TYPE_WIDENED
        assert ev.severity is Severity.LOW

    def test_widened_varchar_capacity_growth(self):
        b = _col(data_type="varchar(20)", character_max_length=20)
        a = _col(data_type="varchar(50)", character_max_length=50)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.TYPE_WIDENED

    def test_narrowed_bigint_to_int_requires_backfill(self):
        b = _col(data_type="bigint", nullable=False)
        a = _col(data_type="integer", nullable=False)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.TYPE_NARROWED
        assert ev.severity is Severity.HIGH
        assert ev.requires_backfill is True

    def test_narrowed_varchar_capacity_shrink(self):
        b = _col(data_type="varchar(100)", character_max_length=100)
        a = _col(data_type="varchar(10)", character_max_length=10)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.TYPE_NARROWED

    def test_incompatible_text_to_int(self):
        b = _col(data_type="text")
        a = _col(data_type="integer")
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.TYPE_INCOMPATIBLE
        assert ev.severity is Severity.HIGH


# ---------------------------------------------------------------------------
# COLUMN_RENAMED  (requires batch correlation)
# ---------------------------------------------------------------------------


class TestColumnRenamed:
    def test_drop_plus_add_same_type_pairs_into_rename(self):
        before = _col(name="user_email", data_type="text")
        after = _col(name="email_address", data_type="text")
        evs = Classifier().classify_batch(
            [
                _raw("removed", column_before=before),
                _raw("added", column_after=after),
            ]
        )
        assert len(evs) == 1
        assert evs[0].change_type is ChangeType.COLUMN_RENAMED
        assert evs[0].column_before == before
        assert evs[0].column_after == after
        assert evs[0].severity is Severity.HIGH
        assert any("renamed from" in n for n in evs[0].notes)

    def test_drop_plus_add_different_type_does_not_pair(self):
        evs = Classifier().classify_batch(
            [
                _raw("removed", column_before=_col(name="a", data_type="text")),
                _raw("added", column_after=_col(name="b", data_type="integer")),
            ]
        )
        kinds = {e.change_type for e in evs}
        assert ChangeType.COLUMN_RENAMED not in kinds
        assert ChangeType.COLUMN_DROPPED in kinds
        assert ChangeType.COLUMN_ADDED_NULLABLE in kinds


# ---------------------------------------------------------------------------
# PRECISION_CHANGED
# ---------------------------------------------------------------------------


class TestPrecisionChanged:
    def test_numeric_precision_growth(self):
        b = _col(data_type="numeric", numeric_precision=10, numeric_scale=2)
        a = _col(data_type="numeric", numeric_precision=14, numeric_scale=4)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.PRECISION_CHANGED
        assert ev.severity is Severity.MEDIUM

    def test_numeric_scale_only_shift(self):
        b = _col(data_type="numeric", numeric_precision=10, numeric_scale=2)
        a = _col(data_type="numeric", numeric_precision=10, numeric_scale=4)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.PRECISION_CHANGED


# ---------------------------------------------------------------------------
# PK_CHANGED
# ---------------------------------------------------------------------------


class TestPKChanged:
    def test_pk_flag_added(self):
        b = _col(name="id", data_type="integer", is_primary_key=False)
        a = _col(name="id", data_type="integer", is_primary_key=True)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.PK_CHANGED
        assert ev.severity is Severity.HIGH

    def test_pk_flag_removed(self):
        b = _col(name="id", is_primary_key=True)
        a = _col(name="id", is_primary_key=False)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.PK_CHANGED


# ---------------------------------------------------------------------------
# ENUM_VALUE_ADDED
# ---------------------------------------------------------------------------


class TestEnumValueAdded:
    def test_enum_value_added(self):
        b = _col(data_type="status_enum", enum_values=("new", "shipped"))
        a = _col(data_type="status_enum", enum_values=("new", "shipped", "delivered"))
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.ENUM_VALUE_ADDED
        assert ev.severity is Severity.LOW

    def test_enum_value_removed_does_not_classify_as_added(self):
        b = _col(data_type="status_enum", enum_values=("new", "shipped", "delivered"))
        a = _col(data_type="status_enum", enum_values=("new", "shipped"))
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        # Not a strict subset → falls through, no event.
        assert ev is None or ev.change_type is not ChangeType.ENUM_VALUE_ADDED


# ---------------------------------------------------------------------------
# DEFAULT_CHANGED
# ---------------------------------------------------------------------------


class TestDefaultChanged:
    def test_default_added(self):
        b = _col(default=None)
        a = _col(default="'pending'")
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.DEFAULT_CHANGED

    def test_default_removed(self):
        b = _col(default="'pending'")
        a = _col(default=None)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.DEFAULT_CHANGED


# ---------------------------------------------------------------------------
# NULLABILITY_TIGHTENED
# ---------------------------------------------------------------------------


class TestNullabilityTightened:
    def test_nullable_true_to_false(self):
        b = _col(nullable=True)
        a = _col(nullable=False)
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        assert ev
        assert ev.change_type is ChangeType.NULLABILITY_TIGHTENED
        assert ev.requires_backfill is True

    def test_nullable_false_to_true_is_not_tightening(self):
        b = _col(nullable=False, default="'x'")
        a = _col(nullable=True, default="'x'")
        ev = Classifier().classify(_raw("modified", column_before=b, column_after=a))
        # That direction is a *relaxation* — we ignore it (returns None).
        assert ev is None or ev.change_type is not ChangeType.NULLABILITY_TIGHTENED


# ---------------------------------------------------------------------------
# Table-level diff kinds & fall-through
# ---------------------------------------------------------------------------


class TestTableLevelAndFallthrough:
    def test_table_added_returns_none(self):
        ev = Classifier().classify(_raw("table_added"))
        assert ev is None

    def test_table_removed_returns_none(self):
        ev = Classifier().classify(_raw("table_removed"))
        assert ev is None

    def test_batch_handles_modified_alongside_pairs(self):
        evs = Classifier().classify_batch(
            [
                _raw("removed", column_before=_col(name="old", data_type="text")),
                _raw("added", column_after=_col(name="new", data_type="text")),
                _raw(
                    "modified",
                    column_before=_col(name="amt", data_type="integer", nullable=False),
                    column_after=_col(name="amt", data_type="bigint", nullable=False),
                ),
            ]
        )
        kinds = {e.change_type for e in evs}
        assert kinds == {ChangeType.COLUMN_RENAMED, ChangeType.TYPE_WIDENED}
