"""Tests for the (dry-run) GitHub PR gateway."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from schema_drift.models import FilePatch, MigrationBundle
from schema_drift.pr import GitHubPRGateway, labels_for


def _bundle(**over) -> MigrationBundle:
    base = {
        "drift_event_id": "01HJ-test",
        "branch_name": "drift/01hj-test",
        "pr_title": "Add nullable column `x` to `s.t`",
        "pr_body_markdown": "## body\n",
        "files": (FilePatch(path="models/sources.yml", content="version: 2\n", mode="update"),),
        "labels": ("schema-drift", "severity:low"),
    }
    base.update(over)
    return MigrationBundle(**base)


class TestGatewayDryRun:
    def test_prints_metadata_and_returns_result(self):
        buf = io.StringIO()
        console = Console(file=buf, width=120, force_terminal=False)
        gateway = GitHubPRGateway(repo="o/r", console=console)
        result = gateway.open_pr(_bundle(), dry_run=True)

        out = buf.getvalue()
        assert "drift/01hj-test" in out
        assert "Add nullable column" in out
        assert "schema-drift" in out
        assert "models/sources.yml" in out

        assert result.dry_run is True
        assert result.url is None
        assert result.branch == "drift/01hj-test"

    def test_live_path_not_implemented_yet(self):
        gateway = GitHubPRGateway(repo="o/r")
        with pytest.raises(NotImplementedError, match="Week 1 day 5"):
            gateway.open_pr(_bundle(), dry_run=False)


class TestLabels:
    def test_dedup_extras(self):
        b = _bundle()
        assert labels_for(b, extra=("schema-drift", "needs-human")) == (
            "schema-drift",
            "severity:low",
            "needs-human",
        )
