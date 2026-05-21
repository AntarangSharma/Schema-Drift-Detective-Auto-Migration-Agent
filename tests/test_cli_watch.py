"""CLI-level tests for `drift watch`.

The happy-path watch requires a live Postgres — that lives in the integration
suite. These tests pin the *failure / guidance* surfaces only, which is the
part recruiters and CI exercise hundreds of times more than the live path.
"""

from __future__ import annotations

from typer.testing import CliRunner

from schema_drift.cli import app


def test_watch_without_once_flag_exits_with_guidance():
    result = CliRunner().invoke(app, ["watch"])
    # Exit 2 = "you almost certainly didn't mean this" — distinct from 0/1
    # so wrapping scripts can branch on it.
    assert result.exit_code == 2
    assert "Week 6" in result.stdout or "Week 6" in (result.stderr or "")


def test_watch_help_lists_all_options():
    # Force a wide terminal so Rich/typer doesn't line-wrap option flags and
    # split them across lines (which makes naive `"--foo" in stdout` flaky on
    # CI's narrow default).
    result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    # Match on the bare flag identifier, not the leading dashes — survives
    # whatever wrapping any terminal width imposes.
    for opt in ("once", "dsn", "schemas", "source-identifier"):
        assert opt in result.stdout, f"--{opt} missing from `drift watch --help`"
