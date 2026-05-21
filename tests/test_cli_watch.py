"""CLI-level tests for `drift watch`.

The happy-path watch requires a live Postgres — that lives in the integration
suite. These tests pin the *failure / guidance* surfaces only, which is the
part recruiters and CI exercise hundreds of times more than the live path.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from schema_drift.cli import app


def test_watch_without_once_flag_exits_with_guidance():
    result = CliRunner().invoke(app, ["watch"])
    # Exit 2 = "you almost certainly didn't mean this" — distinct from 0/1
    # so wrapping scripts can branch on it.
    assert result.exit_code == 2
    assert "Week 6" in result.stdout or "Week 6" in (result.stderr or "")


def test_watch_command_declares_expected_options():
    """Introspect the click command instead of scraping help output.

    Help rendering goes through Rich, which line-wraps based on terminal
    width and ANSI styling. Asserting on substrings of rendered help is
    fragile across local vs. CI. The click ``Command`` object is the
    source of truth — assert on it directly.
    """
    click_cmd = typer.main.get_command(app)
    # Resolve the `watch` subcommand. ``get_command`` returns a Group when
    # the typer app has multiple commands.
    assert click_cmd is not None
    watch = click_cmd.commands["watch"]  # type: ignore[attr-defined]

    declared = {opt for p in watch.params for opt in getattr(p, "opts", ())}
    expected = {"--once", "--dsn", "--schemas", "--source-identifier"}
    missing = expected - declared
    assert not missing, f"`drift watch` is missing options {missing}"
