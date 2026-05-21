"""Tests for the GitHub PR gateway.

Dry-run path is tested by rendering into a captured Rich Console.

The live path is tested with a ``FakeGitHubRepo`` injected via the
``repo_factory`` seam. This is more honest than mocking ``github.Github``
itself: we assert on what the gateway *does* (create branch X, commit Y,
open PR Z), not on which PyGithub methods it happens to call.

The opt-in integration test against the real ``drift-demo-sandbox`` repo
lives in ``tests/test_pr_live.py`` and is gated by ``pytest -m live``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import pytest
from github import UnknownObjectException
from rich.console import Console

from schema_drift.models import FilePatch, MigrationBundle
from schema_drift.pr import GitHubPRGateway, GitHubRepoLike, labels_for

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bundle(**over: Any) -> MigrationBundle:
    base: dict[str, Any] = {
        "drift_event_id": "01HJ-test",
        "branch_name": "drift/01hj-test",
        "pr_title": "Add nullable column `x` to `s.t`",
        "pr_body_markdown": "## body\n",
        "files": (FilePatch(path="models/sources.yml", content="version: 2\n", mode="update"),),
        "labels": ("schema-drift", "severity:low"),
    }
    base.update(over)
    return MigrationBundle(**base)


# ---------------------------------------------------------------------------
# FakeGitHubRepo — implements GitHubRepoLike, records every call
# ---------------------------------------------------------------------------


@dataclass
class _FakeContents:
    sha: str
    path: str


@dataclass
class _FakePR:
    html_url: str
    labels: list[str] = field(default_factory=list)

    def add_to_labels(self, *labels: str) -> None:
        self.labels.extend(labels)


@dataclass
class _FakeCommit:
    sha: str


@dataclass
class _FakeBranch:
    commit: _FakeCommit


class FakeGitHubRepo:
    """In-memory stand-in for ``github.Repository.Repository``.

    Only models what the gateway actually exercises. Anything else raises
    so an unexpected dependency surfaces as a loud test failure.
    """

    def __init__(
        self,
        *,
        default_branch: str = "main",
        existing_branches: tuple[str, ...] = ("main",),
        existing_files: dict[str, str] | None = None,
        base_sha: str = "deadbeef",
    ) -> None:
        self.default_branch = default_branch
        self._branches = set(existing_branches)
        self._files: dict[str, str] = dict(existing_files or {})
        self._base_sha = base_sha

        # Call log — tests assert on this.
        self.created_refs: list[tuple[str, str]] = []
        self.created_files: list[tuple[str, str, str]] = []  # (path, branch, content)
        self.updated_files: list[tuple[str, str, str]] = []
        self.deleted_files: list[tuple[str, str]] = []
        self.created_prs: list[_FakePR] = []

    # PyGithub-shaped API ---------------------------------------------------

    def get_branch(self, branch: str) -> _FakeBranch:
        if branch not in self._branches:
            # Mimic PyGithub: raise UnknownObjectException for missing.
            raise UnknownObjectException(404, {"message": "Branch not found"}, {})
        return _FakeBranch(commit=_FakeCommit(sha=self._base_sha))

    def create_git_ref(self, ref: str, sha: str) -> None:
        assert ref.startswith("refs/heads/"), f"unexpected ref shape: {ref!r}"
        branch = ref.removeprefix("refs/heads/")
        self._branches.add(branch)
        self.created_refs.append((branch, sha))

    def get_contents(self, path: str, ref: str = "") -> _FakeContents:
        if path not in self._files:
            raise UnknownObjectException(404, {"message": "Not found"}, {})
        return _FakeContents(sha=f"blobsha-{path}", path=path)

    def create_file(self, path: str, message: str, content: str, branch: str = "") -> None:
        self._files[path] = content
        self.created_files.append((path, branch, content))

    def update_file(
        self, path: str, message: str, content: str, sha: str, branch: str = ""
    ) -> None:
        self._files[path] = content
        self.updated_files.append((path, branch, content))

    def delete_file(self, path: str, message: str, sha: str, branch: str = "") -> None:
        self._files.pop(path, None)
        self.deleted_files.append((path, branch))

    def create_pull(
        self, title: str, body: str, head: str, base: str, draft: bool = False
    ) -> _FakePR:
        pr = _FakePR(html_url=f"https://github.com/o/r/pull/{len(self.created_prs) + 1}")
        # Mirror the parameters into instance attrs the tests assert on.
        pr.__dict__.update(
            {"title": title, "body": body, "head": head, "base": base, "draft": draft}
        )
        self.created_prs.append(pr)
        return pr


# Mypy/pyright sanity check: confirm the fake satisfies the Protocol.
_: GitHubRepoLike = FakeGitHubRepo()


# ---------------------------------------------------------------------------
# Dry-run tests (unchanged behaviour)
# ---------------------------------------------------------------------------


class TestGatewayDryRun:
    def test_prints_metadata_and_returns_result(self) -> None:
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

    def test_renders_backfill_and_rollback_when_present(self) -> None:
        """The rendering branches for backfill/rollback SQL are real UX —
        reviewers see this output in dry-run mode and a refactor silently
        dropping them would be a regression."""
        buf = io.StringIO()
        console = Console(file=buf, width=120, force_terminal=False)
        gateway = GitHubPRGateway(repo="o/r", console=console)
        gateway.open_pr(
            _bundle(
                backfill_sql="UPDATE source_raw.orders SET discount_code = NULL;",
                rollback_sql="ALTER TABLE source_raw.orders DROP COLUMN discount_code;",
            ),
            dry_run=True,
        )
        out = buf.getvalue()
        assert "Backfill SQL" in out
        assert "UPDATE source_raw.orders" in out
        assert "Rollback SQL" in out
        assert "DROP COLUMN discount_code" in out


# ---------------------------------------------------------------------------
# Live-path tests — FakeGitHubRepo, no network
# ---------------------------------------------------------------------------


class TestLivePathSafety:
    def test_refuses_without_drift_live_pr_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DRIFT_LIVE_PR", raising=False)
        fake = FakeGitHubRepo()
        gateway = GitHubPRGateway(repo="o/r", token="t", repo_factory=lambda slug, token: fake)
        with pytest.raises(RuntimeError, match="DRIFT_LIVE_PR is not set"):
            gateway.open_pr(_bundle(), dry_run=False)
        # And it must NOT have touched the fake.
        assert fake.created_refs == []
        assert fake.created_prs == []

    def test_refuses_without_repo_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        gateway = GitHubPRGateway(repo=None, token="t")
        with pytest.raises(RuntimeError, match="repo is required"):
            gateway.open_pr(_bundle(), dry_run=False)

    def test_refuses_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        gateway = GitHubPRGateway(repo="o/r", token=None)
        with pytest.raises(RuntimeError, match="No GitHub token"):
            gateway.open_pr(_bundle(), dry_run=False)


class TestLivePathHappy:
    def test_happy_path_update_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        fake = FakeGitHubRepo(
            existing_files={"models/sources.yml": "old content\n"},
        )
        gateway = GitHubPRGateway(repo="o/r", token="t", repo_factory=lambda slug, token: fake)
        result = gateway.open_pr(_bundle(), dry_run=False)

        # Branch was cut from default branch's tip.
        assert ("drift/01hj-test", "deadbeef") in fake.created_refs
        # File was updated, not created/deleted.
        # ``DriftModel`` strips trailing whitespace on ingest, so the
        # committed content is ``"version: 2"`` (no trailing newline).
        assert fake.updated_files == [("models/sources.yml", "drift/01hj-test", "version: 2")]
        assert fake.created_files == []
        assert fake.deleted_files == []
        # PR was opened against the default branch as a draft (default).
        assert len(fake.created_prs) == 1
        pr = fake.created_prs[0]
        assert pr.head == "drift/01hj-test"  # type: ignore[attr-defined]
        assert pr.base == "main"  # type: ignore[attr-defined]
        assert pr.draft is True  # type: ignore[attr-defined]
        # Labels were applied.
        assert pr.labels == ["schema-drift", "severity:low"]
        # Result reflects live path.
        assert result.dry_run is False
        assert result.url == "https://github.com/o/r/pull/1"
        assert result.skipped_reason is None

    def test_respects_repo_default_branch_master(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Old GH accounts still default to 'master'. The gateway must honour
        # repo.default_branch, NOT hard-code 'main'.
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        fake = FakeGitHubRepo(
            default_branch="master",
            existing_branches=("master",),
            existing_files={"models/sources.yml": "old\n"},
        )
        gateway = GitHubPRGateway(repo="o/r", token="t", repo_factory=lambda slug, token: fake)
        gateway.open_pr(_bundle(), dry_run=False)
        assert fake.created_prs[0].base == "master"  # type: ignore[attr-defined]

    def test_explicit_base_branch_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        fake = FakeGitHubRepo(
            existing_branches=("main", "develop"),
            existing_files={"models/sources.yml": "old\n"},
        )
        gateway = GitHubPRGateway(
            repo="o/r",
            token="t",
            base_branch="develop",
            repo_factory=lambda slug, token: fake,
        )
        gateway.open_pr(_bundle(), dry_run=False)
        assert fake.created_prs[0].base == "develop"  # type: ignore[attr-defined]

    def test_create_mode_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        fake = FakeGitHubRepo()
        bundle = _bundle(
            files=(FilePatch(path="models/new.sql", content="select 1\n", mode="create"),),
        )
        gateway = GitHubPRGateway(repo="o/r", token="t", repo_factory=lambda slug, token: fake)
        gateway.open_pr(bundle, dry_run=False)
        assert fake.created_files == [("models/new.sql", "drift/01hj-test", "select 1")]
        assert fake.updated_files == []

    def test_delete_mode_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        fake = FakeGitHubRepo(existing_files={"models/old.sql": "select 1\n"})
        bundle = _bundle(
            files=(FilePatch(path="models/old.sql", content="", mode="delete"),),
        )
        gateway = GitHubPRGateway(repo="o/r", token="t", repo_factory=lambda slug, token: fake)
        gateway.open_pr(bundle, dry_run=False)
        assert fake.deleted_files == [("models/old.sql", "drift/01hj-test")]

    def test_no_labels_doesnt_call_add_to_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        fake = FakeGitHubRepo(existing_files={"models/sources.yml": "old\n"})
        bundle = _bundle(labels=())
        gateway = GitHubPRGateway(repo="o/r", token="t", repo_factory=lambda slug, token: fake)
        gateway.open_pr(bundle, dry_run=False)
        assert fake.created_prs[0].labels == []


class TestLivePathIdempotency:
    def test_branch_already_exists_skips_without_force_push(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DRIFT_LIVE_PR", "1")
        fake = FakeGitHubRepo(
            existing_branches=("main", "drift/01hj-test"),
            existing_files={"models/sources.yml": "old\n"},
        )
        gateway = GitHubPRGateway(repo="o/r", token="t", repo_factory=lambda slug, token: fake)
        result = gateway.open_pr(_bundle(), dry_run=False)

        assert result.dry_run is False
        assert result.url is None
        assert result.skipped_reason == "branch_exists"

        # Crucially: no writes happened.
        assert fake.created_refs == []
        assert fake.updated_files == []
        assert fake.created_files == []
        assert fake.deleted_files == []
        assert fake.created_prs == []


# ---------------------------------------------------------------------------
# labels_for
# ---------------------------------------------------------------------------


class TestLabels:
    def test_dedup_extras(self) -> None:
        b = _bundle()
        assert labels_for(b, extra=("schema-drift", "needs-human")) == (
            "schema-drift",
            "severity:low",
            "needs-human",
        )
