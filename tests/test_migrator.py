"""Tests for the deterministic migration drafter (Day-3 path)."""

from __future__ import annotations

from pathlib import Path

import pytest

from schema_drift.classifier import Classifier
from schema_drift.migrator import MigrationDrafter
from schema_drift.models import (
    ChangeType,
    ColumnSpec,
    DriftEvent,
    ImpactSet,
    RawChange,
    Severity,
    SourceKind,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SOURCES_YML = """\
version: 2

sources:
  - name: source_raw
    description: "Raw operational tables."
    schema: source_raw
    tables:
      - name: orders
        description: "Order header records."
        columns:
          - name: order_id
            description: "Primary key."
            data_tests: [unique, not_null]
          - name: amount
            description: "Order total in USD."
"""


def _write_dbt_project(root: Path) -> Path:
    (root / "models").mkdir(parents=True)
    (root / "models" / "sources.yml").write_text(_SOURCES_YML)
    return root


def _new_col() -> ColumnSpec:
    return ColumnSpec(
        name="discount_code",
        data_type="text",
        nullable=True,
        ordinal_position=6,
    )


def _make_event() -> DriftEvent:
    raw = RawChange(
        table_identifier="source_raw.orders",
        kind="added",
        column_after=_new_col(),
    )
    ev = Classifier().classify(raw)
    assert ev is not None  # for the typechecker
    return ev


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrationDrafter:
    def test_happy_path_patches_yaml_and_produces_pr_text(self, tmp_path: Path) -> None:
        proj = _write_dbt_project(tmp_path / "dbt_project")
        drafter = MigrationDrafter(dbt_project_dir=proj)
        event = _make_event()
        impact = ImpactSet(dbt_models=("stg_orders",), blast_radius_score=1.0)

        bundle = drafter.draft(event, impact)

        # File patch points at the right path and adds the new column to YAML.
        assert len(bundle.files) == 1
        fp = bundle.files[0]
        assert fp.path.endswith("dbt_project/models/sources.yml")
        assert fp.mode == "update"
        assert "discount_code" in fp.content
        assert "TODO(reviewer)" in fp.content

        # PR text contains the salient facts.
        assert "discount_code" in bundle.pr_body_markdown
        assert "source_raw.orders" in bundle.pr_body_markdown
        assert "stg_orders" in bundle.pr_body_markdown

        # Labels are correct and deterministic.
        assert "schema-drift" in bundle.labels
        assert f"severity:{event.severity.value}" in bundle.labels

        # Defaults: draft + no LLM cost (rules path).
        assert bundle.is_draft is True
        assert bundle.llm_cost_usd == 0.0

    def test_idempotent_when_column_already_present(self, tmp_path: Path) -> None:
        proj = _write_dbt_project(tmp_path / "dbt_project")
        # Pre-add the column so the patcher sees it.
        sources = proj / "models" / "sources.yml"
        sources.write_text(
            sources.read_text().replace(
                "- name: amount",
                "- name: discount_code\n          - name: amount",
            )
        )
        drafter = MigrationDrafter(dbt_project_dir=proj)
        bundle = drafter.draft(_make_event(), ImpactSet())

        # discount_code appears exactly once in the patched YAML.
        assert bundle.files[0].content.count("- name: discount_code") == 1

    def test_unsupported_change_type_raises(self, tmp_path: Path) -> None:
        proj = _write_dbt_project(tmp_path / "dbt_project")
        drafter = MigrationDrafter(dbt_project_dir=proj)

        # Hand-craft a non-nullable-add event (skip the classifier).
        event = DriftEvent(
            source_system=SourceKind.POSTGRES,
            source_identifier="source_raw.orders.id",
            change_type=ChangeType.COLUMN_DROPPED,
            severity=Severity.HIGH,
        )
        with pytest.raises(NotImplementedError, match="COLUMN_ADDED_NULLABLE"):
            drafter.draft(event, ImpactSet())

    def test_missing_table_in_sources_yml_raises(self, tmp_path: Path) -> None:
        proj = tmp_path / "dbt_project"
        (proj / "models").mkdir(parents=True)
        (proj / "models" / "sources.yml").write_text(
            "version: 2\nsources:\n  - name: source_raw\n    schema: source_raw\n    tables: []\n"
        )
        drafter = MigrationDrafter(dbt_project_dir=proj)
        with pytest.raises(ValueError, match="does not declare"):
            drafter.draft(_make_event(), ImpactSet())

    def test_malformed_source_identifier_raises(self, tmp_path: Path) -> None:
        """A two-part identifier (no column) should fail loud — silently
        skipping makes the resulting PR ship with a wrong path."""
        proj = _write_dbt_project(tmp_path / "dbt_project")
        drafter = MigrationDrafter(dbt_project_dir=proj)
        event = DriftEvent(
            source_system=SourceKind.POSTGRES,
            source_identifier="source_raw.orders",  # missing .column
            change_type=ChangeType.COLUMN_ADDED_NULLABLE,
            severity=Severity.LOW,
            column_after=_new_col(),
        )
        with pytest.raises(ValueError, match=r"schema\.table\.column"):
            drafter.draft(event, ImpactSet())

    def test_event_without_column_after_raises(self, tmp_path: Path) -> None:
        """A nullable-add event with no ``column_after`` should fail
        rather than emit a patch with a missing column name."""
        proj = _write_dbt_project(tmp_path / "dbt_project")
        drafter = MigrationDrafter(dbt_project_dir=proj)
        event = DriftEvent(
            source_system=SourceKind.POSTGRES,
            source_identifier="source_raw.orders.discount_code",
            change_type=ChangeType.COLUMN_ADDED_NULLABLE,
            severity=Severity.LOW,
            # column_after omitted on purpose.
        )
        with pytest.raises(ValueError, match="has no column_after"):
            drafter.draft(event, ImpactSet())

    def test_locate_table_skips_unrelated_source_blocks(self, tmp_path: Path) -> None:
        """When ``sources.yml`` declares multiple source blocks, the
        drafter must walk past non-matching schemas and find the right
        table — not bail on the first mismatch."""
        proj = tmp_path / "dbt_project"
        (proj / "models").mkdir(parents=True)
        (proj / "models" / "sources.yml").write_text(
            """\
version: 2

sources:
  - name: marketing_raw
    schema: marketing_raw
    tables:
      - name: campaigns
        columns:
          - name: campaign_id
  - name: source_raw
    schema: source_raw
    tables:
      - name: orders
        columns:
          - name: order_id
"""
        )
        drafter = MigrationDrafter(dbt_project_dir=proj)
        bundle = drafter.draft(_make_event(), ImpactSet())
        # The patch lands under source_raw.orders, not under marketing_raw.
        patched = bundle.files[0].content
        # Marketing block was passed through untouched.
        assert "marketing_raw" in patched
        assert "discount_code" in patched
        # The new column belongs to the orders table, not campaigns.
        marketing_section = patched.split("- name: source_raw", 1)[0]
        assert "discount_code" not in marketing_section
