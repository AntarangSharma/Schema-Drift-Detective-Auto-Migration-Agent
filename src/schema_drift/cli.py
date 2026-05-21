"""Command-line entry point for the drift agent.

Usage::

    drift --help
    drift version
    drift demo --dry-run
"""

from typing import Annotated

import typer
from rich.console import Console

from schema_drift import __version__

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
    """Run the end-to-end demo: inject a drift and open (or simulate) a PR.

    Day-3 milestone target. Currently a stub; will be wired up in Week 1.
    """
    console.print("[yellow]demo command is a stub — wiring up in Week 1[/yellow]")
    console.print(f"dry_run = {dry_run}")
    raise typer.Exit(code=0)


@app.command()
def watch(
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single poll cycle and exit."),
    ] = False,
) -> None:
    """Start the polling loop against configured sources."""
    console.print("[yellow]watch command is a stub — wiring up in Week 1[/yellow]")
    console.print(f"once = {once}")
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
