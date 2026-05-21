"""End-to-end Day-3 demo orchestration.

Constructs a synthetic ``before`` / ``after`` snapshot pair (so the demo runs
without a live Postgres or a freshly-compiled dbt manifest), runs the diff →
classify → impact → draft → render pipeline, and prints the simulated PR
to stdout via :class:`GitHubPRGateway` in dry-run mode.

If a real dbt ``manifest.json`` is available on disk (env var
``DRIFT_MANIFEST_PATH`` or default ``dbt_project/target/manifest.json``), the
lineage stage uses it. Otherwise it falls back to a hardcoded impact set that
matches the dbt models we ship in the repo (``stg_orders``, ``fct_orders``,
``mart_revenue_daily``) so the demo always produces an interesting output.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console

from schema_drift.classifier import Classifier
from schema_drift.lineage import LineageGraph
from schema_drift.migrator import MigrationDrafter
from schema_drift.models import (
    ColumnSpec,
    DriftEvent,
    ImpactSet,
    SchemaSnapshot,
    SourceKind,
    TableSnapshot,
)
from schema_drift.pr import GitHubPRGateway
from schema_drift.watcher.base import SourceWatcher

# ---------------------------------------------------------------------------
# Fixture data: matches infra/seed_demo_data.sql + dbt_project/models/sources.yml
# ---------------------------------------------------------------------------

_BEFORE_ORDERS_COLS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        name="order_id", data_type="int8", nullable=False, ordinal_position=1, is_primary_key=True
    ),
    ColumnSpec(name="customer_id", data_type="int8", nullable=False, ordinal_position=2),
    ColumnSpec(
        name="amount",
        data_type="numeric",
        nullable=False,
        ordinal_position=3,
        numeric_precision=10,
        numeric_scale=2,
    ),
    ColumnSpec(name="status", data_type="text", nullable=False, ordinal_position=4),
    ColumnSpec(name="created_at", data_type="timestamptz", nullable=False, ordinal_position=5),
)

_NEW_COLUMN: ColumnSpec = ColumnSpec(
    name="discount_code",
    data_type="text",
    nullable=True,
    ordinal_position=6,
)


def _make_snapshot(columns: tuple[ColumnSpec, ...]) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_kind=SourceKind.POSTGRES,
        source_identifier="postgres",
        tables=(
            TableSnapshot(
                table_identifier="source_raw.orders",
                columns=columns,
                primary_key=("order_id",),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Lineage fallback (no manifest.json available)
# ---------------------------------------------------------------------------


def _fallback_impact() -> ImpactSet:
    """Pretend we ran a real BFS — used when no compiled manifest is present.

    Matches the dbt models declared under ``dbt_project/models``.
    """
    return ImpactSet(
        dbt_models=("fct_orders", "mart_revenue_daily", "stg_orders"),
        blast_radius_score=3.0,
        lineage_confidence="medium",
        fan_out_conservative=False,
    )


def _resolve_impact(source_identifier: str) -> ImpactSet:
    manifest_env = os.getenv("DRIFT_MANIFEST_PATH")
    default = Path("dbt_project/target/manifest.json")
    manifest = Path(manifest_env) if manifest_env else default
    if not manifest.exists():
        return _fallback_impact()
    return LineageGraph.from_manifest(manifest).impact(source_identifier)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_demo(*, dry_run: bool = True, console: Console | None = None) -> int:
    """Execute the Day-3 thin-slice pipeline. Returns a shell exit code."""
    c = console or Console()

    c.rule("[bold magenta]Schema-Drift Detective — Day-3 demo")
    c.print("[dim]1/5 capturing 'before' snapshot…")
    before = _make_snapshot(_BEFORE_ORDERS_COLS)

    c.print("[dim]2/5 injecting drift (ALTER TABLE ADD COLUMN discount_code TEXT NULL)…")
    after = _make_snapshot((*_BEFORE_ORDERS_COLS, _NEW_COLUMN))

    c.print("[dim]3/5 diffing snapshots…")
    raw_changes = SourceWatcher.diff(before, after)
    if not raw_changes:
        c.print("[red]No changes detected — nothing to do.")
        return 1

    c.print(f"[dim]4/5 classifying {len(raw_changes)} raw change(s)…")
    classifier = Classifier()
    events: list[DriftEvent] = []
    for rc in raw_changes:
        ev = classifier.classify(rc)
        if ev is not None:
            events.append(ev)
    if not events:
        c.print("[red]Classifier produced no events.")
        return 1

    event = events[0]
    impact = _resolve_impact(event.source_identifier)
    # Re-wrap with the resolved impact so the bundle's PR body shows it.
    event_with_impact = event.model_copy(update={"impact": impact})

    c.print(
        f"[green]✓ event[/green] "
        f"{event_with_impact.change_type.value} "
        f"({event_with_impact.severity.value}) "
        f"→ {len(impact.dbt_models)} downstream model(s)"
    )

    c.print("[dim]5/5 drafting migration bundle…")
    drafter = MigrationDrafter()
    bundle = drafter.draft(event_with_impact, impact)

    gateway = GitHubPRGateway(
        repo=os.getenv("DRIFT_GITHUB_REPO"),
        console=c,
        # Token is only consulted on the live path; safe to leave None for
        # dry-runs.
        token=os.getenv("DRIFT_GITHUB_TOKEN"),
    )
    result = gateway.open_pr(bundle, dry_run=dry_run)
    if not dry_run and result.url:
        c.print(f"[bold green]✓ opened PR[/bold green] [link={result.url}]{result.url}[/link]")
    elif not dry_run and result.skipped_reason == "branch_exists":
        c.print(
            f"[yellow]↷ skipped[/yellow] branch [bold]{result.branch}[/bold] "
            "already exists — leaving the reviewer's edits alone."
        )
    return 0
