"""Command-line entry point for the drift agent.

Usage::

    drift --help
    drift version
    drift demo --dry-run
    drift watch --once
"""

import os
from typing import Annotated

import typer
from rich.console import Console

from schema_drift import __version__
from schema_drift.demo import run_demo
from schema_drift.runner import WatcherRunner
from schema_drift.storage import PostgresSnapshotStore
from schema_drift.watcher.postgres import PostgresWatcher

# Default DSN matches the docker-compose Postgres. Override with $DRIFT_DSN.
_DEFAULT_DSN = "postgresql://drift:drift@localhost:55432/drift"

app = typer.Typer(
    name="drift",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Schema Drift Detective — upstream schema-drift CI check.",
)
console = Console()


@app.command()
def version() -> None:
    """Print the installed package version."""
    console.print(f"schema-drift-detective [bold]{__version__}[/bold]")


@app.command()
def demo(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Print the PR body to stdout instead of opening a real PR.",
        ),
    ] = True,
) -> None:
    """Run the end-to-end demo: inject a drift and open (or simulate) a PR."""
    code = run_demo(dry_run=dry_run, console=console)
    raise typer.Exit(code=code)


@app.command()
def watch(
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single poll cycle and exit."),
    ] = False,
    dsn: Annotated[
        str | None,
        typer.Option(
            "--dsn",
            help="Postgres DSN. Defaults to $DRIFT_DSN or the docker-compose URL.",
        ),
    ] = None,
    schemas: Annotated[
        str,
        typer.Option(
            "--schemas",
            help="Comma-separated list of schemas to monitor.",
        ),
    ] = "source_raw",
    source_identifier: Annotated[
        str,
        typer.Option(
            "--source-identifier",
            help="Label written to snapshots / drift events for this source.",
        ),
    ] = "postgres",
    source_kind: Annotated[
        str,
        typer.Option(
            "--source-kind",
            help="Type of database source (postgres, snowflake, bigquery).",
        ),
    ] = "postgres",
) -> None:
    """Poll the configured database source once and report any drift events.

    Currently only ``--once`` is wired (Day 4). The long-running daemon mode
    is Week 6 — keep that flag the explicit default so nobody accidentally
    spins up a polling loop without thinking about it.
    """
    if not once:
        console.print("[yellow]Long-running watch loop is Week 6 — pass --once for now.[/yellow]")
        raise typer.Exit(code=2)

    effective_dsn = dsn or os.getenv("DRIFT_DSN", _DEFAULT_DSN)
    schema_list = [s.strip() for s in schemas.split(",") if s.strip()]

    from schema_drift.watcher.bigquery import BigQueryWatcher, BigQueryWatcherConfig
    from schema_drift.watcher.snowflake import SnowflakeWatcher, SnowflakeWatcherConfig

    if source_kind == "postgres":
        watcher = PostgresWatcher(
            dsn=effective_dsn,
            schemas=schema_list,
            source_identifier=source_identifier,
        )
    elif source_kind == "snowflake":
        sf_account = os.getenv("SF_ACCOUNT", "demo_acme")
        sf_database = os.getenv("SF_DATABASE", "ANALYTICS")
        sf_user = os.getenv("SF_USER", "bot")
        sf_role = os.getenv("SF_ROLE", "DRIFT_DETECTIVE")
        sf_warehouse = os.getenv("SF_WAREHOUSE", "WH_XS")
        cfg = SnowflakeWatcherConfig(
            account=sf_account,
            database=sf_database,
            user=sf_user,
            role=sf_role,
            warehouse=sf_warehouse,
            schemas=tuple(schema_list),
            source_identifier=source_identifier,
        )
        watcher = SnowflakeWatcher(cfg)
    elif source_kind == "bigquery":
        bq_project = os.getenv("BQ_PROJECT", "demo-project")
        bq_dataset = os.getenv("BQ_DATASET", schema_list[0] if schema_list else "source_raw")
        bq_location = os.getenv("BQ_LOCATION", None)
        bq_cfg = BigQueryWatcherConfig(
            project=bq_project,
            dataset=bq_dataset,
            location=bq_location,
            source_identifier=source_identifier,
        )
        watcher = BigQueryWatcher(bq_cfg)
    else:
        console.print(f"[red]Unsupported source kind: {source_kind}[/red]")
        raise typer.Exit(code=1)

    store = PostgresSnapshotStore(dsn=effective_dsn)
    runner = WatcherRunner(watcher=watcher, store=store)

    console.print(f"[dim]watching schemas {schema_list} on {effective_dsn}…[/dim]")
    result = runner.run_once()

    if result.is_baseline:
        console.print(
            "[green]✓ baseline snapshot captured[/green] "
            f"(snapshot_id={result.snapshot.snapshot_id})"
        )
        console.print(
            "[dim]first run for this source — nothing to diff against yet. "
            "Re-run after a schema change to see drift events.[/dim]"
        )
        raise typer.Exit(code=0)

    if result.event_count == 0:
        console.print("[green]✓ no drift detected[/green]")
        raise typer.Exit(code=0)

    console.print(f"[bold yellow]⚠ {result.event_count} drift event(s) detected[/bold yellow]")
    for ev in result.events:
        console.print(f"  • [{ev.severity.value}] {ev.change_type.value} → {ev.source_identifier}")
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
