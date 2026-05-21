"""Smoke test for the end-to-end Day-3 demo orchestration."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from schema_drift.demo import run_demo

FIXTURE_MANIFEST = Path(__file__).parent / "fixtures" / "manifest.json"


class TestRunDemo:
    def test_dry_run_returns_zero_and_renders_pr(self, monkeypatch):
        monkeypatch.setenv("DRIFT_MANIFEST_PATH", str(FIXTURE_MANIFEST))
        buf = io.StringIO()
        console = Console(file=buf, width=140, force_terminal=False)

        code = run_demo(dry_run=True, console=console)
        out = buf.getvalue()

        assert code == 0
        assert "discount_code" in out
        # The lineage step found at least one downstream model.
        for expected in ("stg_orders", "fct_orders", "mart_revenue_daily"):
            assert expected in out, f"expected {expected} in demo output"
        assert "DRY-RUN" in out

    def test_dry_run_works_without_manifest(self, monkeypatch, tmp_path):
        # Point to a non-existent manifest so the fallback impact kicks in.
        monkeypatch.setenv("DRIFT_MANIFEST_PATH", str(tmp_path / "nope.json"))
        buf = io.StringIO()
        console = Console(file=buf, width=140, force_terminal=False)

        code = run_demo(dry_run=True, console=console)
        assert code == 0
        assert "discount_code" in buf.getvalue()
