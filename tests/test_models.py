"""Contract tests for ``schema_drift.models``.

These tests are the single most important defensive layer in the codebase:
they pin the wire format that every other module depends on. Treat any
failure here as a breaking change that needs a deliberate review.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schema_drift import models as models_module
from schema_drift.models import (
    DEFAULT_SEVERITY,
    DESTRUCTIVE_CHANGES,
    Action,
    AuditRecord,
    ChangeType,
    ColumnSpec,
    DashboardRef,
    DriftEvent,
    FilePatch,
    ImpactSet,
    MigrationBundle,
    RawChange,
    SchemaSnapshot,
    Severity,
    SourceKind,
    TableSnapshot,
)

# ---------------------------------------------------------------------------
# Helpers (kept minimal — explicit factories beat fixtures for contract tests)
# ---------------------------------------------------------------------------


def _col(name: str = "id", **overrides) -> ColumnSpec:
    base = {
        "name": name,
        "data_type": "integer",
        "nullable": False,
        "ordinal_position": 1,
    }
    base.update(overrides)
    return ColumnSpec(**base)


def _event(change_type: ChangeType, **overrides) -> DriftEvent:
    severity = overrides.pop("severity", DEFAULT_SEVERITY[change_type])
    base = {
        "source_system": SourceKind.POSTGRES,
        "source_identifier": "source_raw.orders.customer_id",
        "change_type": change_type,
        "severity": severity,
    }
    base.update(overrides)
    return DriftEvent(**base)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestSeverityOrdering:
    def test_low_lt_medium_lt_high(self):
        assert Severity.LOW.rank < Severity.MEDIUM.rank < Severity.HIGH.rank

    def test_str_value(self):
        assert Severity.LOW.value == "low"
        assert Severity.HIGH.value == "high"


class TestChangeTypeRegistry:
    def test_every_change_type_has_default_severity(self):
        missing = set(ChangeType) - set(DEFAULT_SEVERITY)
        assert missing == set(), f"DEFAULT_SEVERITY missing entries for {missing}"

    def test_destructive_changes_are_high_severity_by_default(self):
        for ct in DESTRUCTIVE_CHANGES:
            assert DEFAULT_SEVERITY[ct] == Severity.HIGH

    def test_destructive_is_disjoint_from_nondestructive_lows(self):
        lows = {ct for ct, sev in DEFAULT_SEVERITY.items() if sev == Severity.LOW}
        assert lows.isdisjoint(DESTRUCTIVE_CHANGES)


# ---------------------------------------------------------------------------
# ColumnSpec
# ---------------------------------------------------------------------------


class TestColumnSpec:
    def test_minimum_required_fields(self):
        c = ColumnSpec(name="x", data_type="text", nullable=True, ordinal_position=0)
        assert c.is_primary_key is False
        assert c.default is None

    def test_name_cannot_be_empty(self):
        with pytest.raises(ValidationError):
            ColumnSpec(name="", data_type="text", nullable=True, ordinal_position=0)

    def test_negative_ordinal_rejected(self):
        with pytest.raises(ValidationError):
            ColumnSpec(name="x", data_type="text", nullable=True, ordinal_position=-1)

    def test_frozen(self):
        c = _col()
        with pytest.raises(ValidationError):
            c.name = "different"  # type: ignore[misc]

    def test_extra_fields_rejected(self):
        # Deliberately pass an undeclared kwarg; pydantic should reject at runtime.
        kwargs: dict = {  # use a dict so pyright doesn't see "extra" as a literal kwarg
            "name": "x",
            "data_type": "text",
            "nullable": True,
            "ordinal_position": 0,
            "extra": "?",
        }
        with pytest.raises(ValidationError):
            ColumnSpec(**kwargs)


# ---------------------------------------------------------------------------
# RawChange
# ---------------------------------------------------------------------------


class TestRawChange:
    def test_added_requires_column_after(self):
        with pytest.raises(ValidationError, match="column_after"):
            RawChange(table_identifier="orders", kind="added")

    def test_removed_requires_column_before(self):
        with pytest.raises(ValidationError, match="column_before"):
            RawChange(table_identifier="orders", kind="removed")

    def test_modified_requires_both(self):
        with pytest.raises(ValidationError, match="column_before"):
            RawChange(table_identifier="orders", kind="modified", column_after=_col())

    def test_added_happy_path(self):
        rc = RawChange(table_identifier="orders", kind="added", column_after=_col("new_col"))
        assert rc.column_after is not None
        assert rc.column_after.name == "new_col"

    def test_table_added_does_not_require_columns(self):
        # table_added/table_removed are structural events; no column needed.
        rc = RawChange(table_identifier="orders", kind="table_added")
        assert rc.column_before is None
        assert rc.column_after is None


# ---------------------------------------------------------------------------
# DriftEvent (the most important model)
# ---------------------------------------------------------------------------


class TestDriftEventValidators:
    def test_destructive_drop_cannot_auto_merge(self):
        with pytest.raises(ValidationError, match="cannot be auto_mergeable"):
            _event(ChangeType.COLUMN_DROPPED, auto_mergeable=True)

    def test_destructive_narrow_cannot_auto_merge(self):
        with pytest.raises(ValidationError, match="cannot be auto_mergeable"):
            _event(ChangeType.TYPE_NARROWED, auto_mergeable=True)

    def test_nondestructive_can_auto_merge(self):
        ev = _event(ChangeType.COLUMN_ADDED_NULLABLE, auto_mergeable=True)
        assert ev.auto_mergeable is True

    @pytest.mark.parametrize(
        "destructive",
        [
            pytest.param(ct, id=ct.value)
            for ct in sorted(DESTRUCTIVE_CHANGES, key=lambda c: c.value)
        ],
    )
    def test_all_destructive_types_blocked_from_auto_merge(self, destructive: ChangeType):
        with pytest.raises(ValidationError):
            _event(destructive, auto_mergeable=True)

    def test_severity_cannot_go_below_default(self):
        # COLUMN_DROPPED default is HIGH; LOW must be rejected.
        with pytest.raises(ValidationError, match="below the default"):
            _event(ChangeType.COLUMN_DROPPED, severity=Severity.LOW)

    def test_severity_can_be_upgraded_above_default(self):
        # COLUMN_ADDED_NULLABLE default is LOW; upgrading to HIGH is fine
        # (e.g. because a tier:critical dashboard is affected).
        ev = _event(ChangeType.COLUMN_ADDED_NULLABLE, severity=Severity.HIGH)
        assert ev.severity == Severity.HIGH

    def test_default_severity_used_when_not_specified(self):
        ev = _event(ChangeType.COLUMN_ADDED_NULLABLE)
        assert ev.severity == Severity.LOW

    def test_confidence_clamped_0_to_1(self):
        with pytest.raises(ValidationError):
            _event(ChangeType.COLUMN_ADDED_NULLABLE, confidence=1.5)
        with pytest.raises(ValidationError):
            _event(ChangeType.COLUMN_ADDED_NULLABLE, confidence=-0.1)


class TestDriftEventSerialization:
    def test_round_trip_json(self):
        before = _col("customer_id", nullable=False)
        after = _col("customer_id", data_type="bigint", nullable=False)
        ev = _event(
            ChangeType.TYPE_WIDENED,
            column_before=before,
            column_after=after,
            impact=ImpactSet(dbt_models=("stg_orders", "fct_orders")),
        )
        data = ev.model_dump_json()
        restored = DriftEvent.model_validate_json(data)
        assert restored == ev

    def test_unique_id_per_event(self):
        a = _event(ChangeType.COLUMN_ADDED_NULLABLE)
        b = _event(ChangeType.COLUMN_ADDED_NULLABLE)
        assert a.id != b.id

    def test_detected_at_is_utc(self):
        ev = _event(ChangeType.COLUMN_ADDED_NULLABLE)
        assert ev.detected_at.tzinfo is not None
        assert ev.detected_at.utcoffset() == (ev.detected_at - ev.detected_at).__class__(0)


# ---------------------------------------------------------------------------
# ImpactSet
# ---------------------------------------------------------------------------


class TestImpactSet:
    def test_defaults(self):
        impact = ImpactSet()
        assert impact.dbt_models == ()
        assert impact.dashboards == ()
        assert impact.blast_radius_score == 0.0
        assert impact.fan_out_conservative is False
        assert impact.lineage_confidence == "high"

    def test_negative_score_rejected(self):
        with pytest.raises(ValidationError):
            ImpactSet(blast_radius_score=-1.0)

    def test_dashboard_tier_default(self):
        d = DashboardRef(tool="metabase", id="42", name="Exec Revenue")
        assert d.tier == "normal"


# ---------------------------------------------------------------------------
# SchemaSnapshot
# ---------------------------------------------------------------------------


class TestSchemaSnapshot:
    def test_lookup_table_by_identifier(self):
        t1 = TableSnapshot(table_identifier="source_raw.orders", columns=(_col(),))
        t2 = TableSnapshot(table_identifier="source_raw.customers", columns=(_col("cid"),))
        snap = SchemaSnapshot(
            source_kind=SourceKind.POSTGRES,
            source_identifier="orders_db",
            tables=(t1, t2),
        )
        assert snap.table_by_identifier("source_raw.orders") is t1
        assert snap.table_by_identifier("source_raw.nope") is None

    def test_column_lookup_in_table(self):
        t = TableSnapshot(
            table_identifier="source_raw.orders",
            columns=(_col("id"), _col("customer_id", ordinal_position=2)),
        )
        assert t.column_by_name("customer_id") is not None
        assert t.column_by_name("missing") is None

    def test_snapshot_has_ulid_id_and_utc_timestamp(self):
        snap = SchemaSnapshot(source_kind=SourceKind.POSTGRES, source_identifier="db", tables=())
        assert len(snap.snapshot_id) == 26  # ULID canonical length
        assert snap.captured_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Migration / audit / action
# ---------------------------------------------------------------------------


class TestMigrationBundle:
    def test_defaults_to_draft(self):
        bundle = MigrationBundle(
            drift_event_id="01HJ...",
            branch_name="drift/01HJ",
            pr_title="Add new column",
            pr_body_markdown="# Impact",
            files=(FilePatch(path="models/x.sql", content="select 1", mode="update"),),
        )
        assert bundle.is_draft is True
        assert bundle.llm_cost_usd == 0.0


class TestAuditRecord:
    def test_action_required(self):
        with pytest.raises(ValidationError):
            AuditRecord(actor="x")  # type: ignore[call-arg]

    def test_payload_default(self):
        rec = AuditRecord(actor="PostgresWatcher", action="snapshot_captured")
        assert rec.payload == {}


class TestActionEnum:
    def test_four_actions(self):
        assert {a.value for a in Action} == {"ignore", "alert_only", "open_draft_pr", "open_pr"}


# ---------------------------------------------------------------------------
# Module exports (cheap regression net)
# ---------------------------------------------------------------------------


def test_all_public_names_importable():
    for name in models_module.__all__:
        assert hasattr(models_module, name), f"__all__ promises {name} but it's missing"
