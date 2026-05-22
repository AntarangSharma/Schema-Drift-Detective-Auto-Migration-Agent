"""Pydantic v2 contracts for the schema-drift pipeline.

Every component in the pipeline (watcher → classifier → lineage → policy →
migrator → PR gateway) communicates through these models. Keep them
backwards-compatible; downstream consumers depend on field names.

Conventions
-----------
* All public models inherit from ``DriftModel`` (frozen + strict).
* Enums are ``str``-backed for JSON portability and Postgres compatibility.
* Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ulid import ULID

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class DriftModel(BaseModel):
    """Base config for every model in the pipeline.

    Frozen by default so events behave like values (safe to share across
    pipeline stages without defensive copies). Extra fields rejected so
    typos surface immediately.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


def _utcnow() -> datetime:
    """UTC ``now`` factory. Module-level so it can be monkey-patched in tests."""
    return datetime.now(tz=UTC)


def _new_ulid() -> str:
    """ULID factory; ULIDs sort lexicographically by timestamp."""
    return str(ULID())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SourceKind(StrEnum):
    """Where a monitored schema lives."""

    POSTGRES = "postgres"
    DUCKDB = "duckdb"
    REST_API = "rest_api"
    KAFKA = "kafka"  # stretch (week 7)
    DEBEZIUM = "debezium"
    SNOWFLAKE = "snowflake"


class ChangeType(StrEnum):
    """The 13 schema changes the classifier recognises.

    Severity and remediation policy are derived from this enum + the
    surrounding context (impact set, downstream count). See
    ``DEFAULT_SEVERITY`` below for the per-type baseline.
    """

    COLUMN_ADDED_NULLABLE = "column_added_nullable"
    COLUMN_ADDED_NOT_NULL = "column_added_not_null"
    COLUMN_ADDED_NOT_NULL_NO_DEFAULT = "column_added_not_null_no_default"
    COLUMN_DROPPED = "column_dropped"
    TYPE_WIDENED = "type_widened"
    TYPE_NARROWED = "type_narrowed"
    TYPE_INCOMPATIBLE = "type_incompatible"
    COLUMN_RENAMED = "column_renamed"
    PRECISION_CHANGED = "precision_changed"
    PK_CHANGED = "pk_changed"
    ENUM_VALUE_ADDED = "enum_value_added"
    DEFAULT_CHANGED = "default_changed"
    NULLABILITY_TIGHTENED = "nullability_tightened"
    PARTITION_KEY_CHANGED = "partition_key_changed"


class Severity(StrEnum):
    """Severity bucket. Ordered LOW < MEDIUM < HIGH."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


# Per-ChangeType default severity. The classifier may upgrade based on
# context (e.g. impacted dashboards), but never downgrades from this baseline.
DEFAULT_SEVERITY: dict[ChangeType, Severity] = {
    ChangeType.COLUMN_ADDED_NULLABLE: Severity.LOW,
    ChangeType.COLUMN_ADDED_NOT_NULL: Severity.LOW,
    ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT: Severity.MEDIUM,
    ChangeType.COLUMN_DROPPED: Severity.HIGH,
    ChangeType.TYPE_WIDENED: Severity.LOW,
    ChangeType.TYPE_NARROWED: Severity.HIGH,
    ChangeType.TYPE_INCOMPATIBLE: Severity.HIGH,
    ChangeType.COLUMN_RENAMED: Severity.HIGH,
    ChangeType.PRECISION_CHANGED: Severity.MEDIUM,
    ChangeType.PK_CHANGED: Severity.HIGH,
    ChangeType.ENUM_VALUE_ADDED: Severity.LOW,
    ChangeType.DEFAULT_CHANGED: Severity.MEDIUM,
    ChangeType.NULLABILITY_TIGHTENED: Severity.MEDIUM,
    ChangeType.PARTITION_KEY_CHANGED: Severity.HIGH,
}

# Destructive change types: NEVER auto-mergeable, always require a human.
DESTRUCTIVE_CHANGES: frozenset[ChangeType] = frozenset(
    {
        ChangeType.COLUMN_DROPPED,
        ChangeType.TYPE_NARROWED,
        ChangeType.TYPE_INCOMPATIBLE,
        ChangeType.PK_CHANGED,
        ChangeType.PARTITION_KEY_CHANGED,
    }
)


class Action(StrEnum):
    """The PolicyEngine's decision for a given (DriftEvent, ImpactSet) pair."""

    IGNORE = "ignore"
    ALERT_ONLY = "alert_only"  # Slack ping, no PR
    OPEN_DRAFT_PR = "open_draft_pr"  # PR opened as draft, requires-human label
    OPEN_PR = "open_pr"  # PR opened as ready-for-review (still needs human merge)


# ---------------------------------------------------------------------------
# Schema snapshots
# ---------------------------------------------------------------------------


class ColumnSpec(DriftModel):
    """A column at a single moment in time."""

    name: str = Field(min_length=1, max_length=128)
    data_type: str = Field(min_length=1, max_length=64)
    nullable: bool
    default: str | None = None
    is_primary_key: bool = False
    ordinal_position: int = Field(ge=0)
    character_max_length: int | None = Field(default=None, ge=0)
    numeric_precision: int | None = Field(default=None, ge=0)
    numeric_scale: int | None = Field(default=None, ge=0)
    enum_values: tuple[str, ...] | None = None  # Postgres enum / check-constraint set


class TableSnapshot(DriftModel):
    """All columns of one table at one capture time."""

    table_identifier: str  # e.g. "source_raw.orders"
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...] = ()
    partition_keys: tuple[str, ...] = ()

    def column_by_name(self, name: str) -> ColumnSpec | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None


class SchemaSnapshot(DriftModel):
    """A complete capture of one source system's tables.

    The watcher emits one of these per polling cycle. Diffs are computed
    between consecutive snapshots for the same ``source_identifier``.
    """

    snapshot_id: str = Field(default_factory=_new_ulid)
    source_kind: SourceKind
    source_identifier: str
    captured_at: datetime = Field(default_factory=_utcnow)
    tables: tuple[TableSnapshot, ...]

    def table_by_identifier(self, ident: str) -> TableSnapshot | None:
        for t in self.tables:
            if t.table_identifier == ident:
                return t
        return None


# ---------------------------------------------------------------------------
# Diff output (pre-classification)
# ---------------------------------------------------------------------------


class RawChange(DriftModel):
    """A diff entry between two snapshots, BEFORE classifier interpretation.

    The watcher produces a stream of these. The classifier consumes the
    stream and produces ``DriftEvent`` objects (possibly correlating multiple
    raw changes into one event, e.g. drop+add → rename).
    """

    table_identifier: str
    kind: Literal["added", "removed", "modified", "table_added", "table_removed"]
    column_before: ColumnSpec | None = None
    column_after: ColumnSpec | None = None

    @model_validator(mode="after")
    def _validate_columns_present(self) -> RawChange:
        match self.kind:
            case "added" if self.column_after is None:
                raise ValueError("'added' requires column_after")
            case "removed" if self.column_before is None:
                raise ValueError("'removed' requires column_before")
            case "modified" if self.column_before is None or self.column_after is None:
                raise ValueError("'modified' requires both column_before and column_after")
        return self


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------


class DashboardRef(DriftModel):
    """A BI asset (Metabase card, Looker tile, …) that depends on a model."""

    tool: Literal["metabase", "looker", "tableau", "marquez"]
    id: str
    name: str
    tier: Literal["critical", "important", "normal"] = "normal"
    url: str | None = None


class MLFeatureRef(DriftModel):
    """A feature-store entity that depends on a model."""

    feature_set: str
    feature_name: str
    online: bool = False  # online vs. offline store


class ImpactSet(DriftModel):
    """Everything downstream of a drift event."""

    dbt_models: tuple[str, ...] = ()
    affected_columns: tuple[tuple[str, str], ...] = ()  # (model, column)
    dashboards: tuple[DashboardRef, ...] = ()
    ml_features: tuple[MLFeatureRef, ...] = ()
    blast_radius_score: float = Field(default=0.0, ge=0.0)
    lineage_confidence: Literal["high", "medium", "low"] = "high"
    fan_out_conservative: bool = False  # True if SELECT * forced conservative widening


# ---------------------------------------------------------------------------
# DriftEvent (the main event object)
# ---------------------------------------------------------------------------


class DriftEvent(DriftModel):
    """A classified schema change with a quantified downstream impact.

    Produced by the classifier (+ lineage). Consumed by the policy engine.
    Persisted to ``schema_drift.drift_events``.
    """

    id: str = Field(default_factory=_new_ulid)
    detected_at: datetime = Field(default_factory=_utcnow)
    source_system: SourceKind
    source_identifier: str  # e.g. "source_raw.orders.customer_id"
    change_type: ChangeType
    severity: Severity
    column_before: ColumnSpec | None = None
    column_after: ColumnSpec | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    impact: ImpactSet = Field(default_factory=ImpactSet)
    auto_mergeable: bool = False  # set to False for any destructive change
    requires_backfill: bool = False
    raw_changes: tuple[RawChange, ...] = ()  # provenance: which RawChange(s) became this
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _enforce_destructive_gate(self) -> DriftEvent:
        if self.change_type in DESTRUCTIVE_CHANGES and self.auto_mergeable:
            raise ValueError(
                f"Destructive change {self.change_type.value!r} cannot be auto_mergeable"
            )
        # Severity floor: never go below the default for this change type.
        default = DEFAULT_SEVERITY[self.change_type]
        if self.severity.rank < default.rank:
            raise ValueError(
                f"Severity {self.severity.value!r} is below the default "
                f"{default.value!r} for {self.change_type.value!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Migration drafting outputs
# ---------------------------------------------------------------------------


class FilePatch(DriftModel):
    """A single file write within a MigrationBundle."""

    path: str  # repo-relative path, e.g. "models/staging/stg_orders.sql"
    content: str
    mode: Literal["create", "update", "delete"]


class MigrationBundle(DriftModel):
    """Everything the PR Gateway needs to open a PR for a single drift event."""

    drift_event_id: str
    branch_name: str
    pr_title: str
    pr_body_markdown: str
    files: tuple[FilePatch, ...]
    rollback_sql: str | None = None
    backfill_sql: str | None = None
    labels: tuple[str, ...] = ()
    is_draft: bool = True  # default-safe: drafts unless policy explicitly opens
    llm_cost_usd: float = Field(default=0.0, ge=0.0)
    llm_tokens_in: int = Field(default=0, ge=0)
    llm_tokens_out: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class AuditRecord(DriftModel):
    """A single audit-log line. Every pipeline action emits at least one."""

    occurred_at: datetime = Field(default_factory=_utcnow)
    actor: str  # component name e.g. "PostgresWatcher" / "MigrationDrafter"
    action: str  # short verb e.g. "snapshot_captured" / "pr_opened"
    target: str | None = None  # e.g. drift_event id, PR url, table identifier
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "DEFAULT_SEVERITY",
    "DESTRUCTIVE_CHANGES",
    "Action",
    "AuditRecord",
    "ChangeType",
    "ColumnSpec",
    "DashboardRef",
    "DriftEvent",
    "DriftModel",
    "FilePatch",
    "ImpactSet",
    "MLFeatureRef",
    "MigrationBundle",
    "RawChange",
    "SchemaSnapshot",
    "Severity",
    "SourceKind",
    "TableSnapshot",
]
