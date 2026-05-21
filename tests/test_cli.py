"""Tests for the typer CLI entry point."""

from __future__ import annotations

from typer.testing import CliRunner

from schema_drift import __version__
from schema_drift.cli import app


def test_version_command_prints_package_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_listed_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("version", "demo", "watch"):
        assert cmd in result.stdout


def test_demo_dry_run_default():
    result = CliRunner().invoke(app, ["demo"])
    assert result.exit_code == 0
    # End-to-end demo prints the dry-run banner and the new column name.
    assert "DRY-RUN" in result.stdout
    assert "discount_code" in result.stdout
