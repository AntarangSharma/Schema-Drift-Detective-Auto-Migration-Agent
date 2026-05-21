"""Tests for the Day-3 rule-based classifier."""

from __future__ import annotations

from schema_drift.classifier import Classifier
from schema_drift.models import ChangeType, ColumnSpec, RawChange, Severity


def _col(name: str = "discount_code", **over) -> ColumnSpec:
    base = {
        "name": name,
        "data_type": "text",
        "nullable": True,
        "ordinal_position": 6,
    }
    base.update(over)
    return ColumnSpec(**base)


class TestClassifierAdded:
    def test_nullable_add(self):
        ev = Classifier().classify(
            RawChange(table_identifier="source_raw.orders", kind="added", column_after=_col())
        )
        assert ev is not None
        assert ev.change_type is ChangeType.COLUMN_ADDED_NULLABLE
        assert ev.severity is Severity.LOW
        assert ev.source_identifier == "source_raw.orders.discount_code"

    def test_not_null_with_default(self):
        ev = Classifier().classify(
            RawChange(
                table_identifier="source_raw.orders",
                kind="added",
                column_after=_col(nullable=False, default="'pending'"),
            )
        )
        assert ev is not None
        assert ev.change_type is ChangeType.COLUMN_ADDED_NOT_NULL

    def test_not_null_without_default(self):
        ev = Classifier().classify(
            RawChange(
                table_identifier="source_raw.orders",
                kind="added",
                column_after=_col(nullable=False, default=None),
            )
        )
        assert ev is not None
        assert ev.change_type is ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT
        assert ev.severity is Severity.MEDIUM


class TestClassifierFallthrough:
    def test_unrecognised_kind_returns_none(self):
        # 'removed' / 'modified' aren't wired in Day-3 — returning None is the spec.
        ev = Classifier().classify(
            RawChange(
                table_identifier="source_raw.orders",
                kind="removed",
                column_before=_col(),
            )
        )
        assert ev is None
