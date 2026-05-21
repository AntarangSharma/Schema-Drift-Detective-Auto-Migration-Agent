"""GitHub PR Gateway.

Two paths live here:

* **Dry-run** (the default): renders the bundle to a Rich console. No network,
  no secrets, safe to run in CI on every push.
* **Live**: actually creates a branch, commits the patches, opens a PR, and
  applies labels via the GitHub REST API (PyGithub 2.x).

Why both paths share a class
----------------------------
The classifier / migrator / runner / tests all target ``GitHubPRGateway``
with ``dry_run=True``. Swapping the live path in *behind* the same interface
keeps the rest of the pipeline ignorant of the rollout state.

Safety
------
The live path refuses to run unless the env var ``DRIFT_LIVE_PR=1`` is set.
This is belt-and-braces: CI defaults to dry-run and we want a second,
**explicit** opt-in before the agent actually mutates a repo.

Idempotency
-----------
If the target branch (``bundle.branch_name``) already exists on the remote,
we **skip** instead of force-pushing. A reviewer might be mid-edit on that
branch; clobbering their commits is the worst thing the agent could do.
The skip is surfaced as ``PRResult(dry_run=False, url=None, branch=...)`` so
the caller can audit it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from github import Auth, Github, UnknownObjectException
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from schema_drift.models import FilePatch, MigrationBundle

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    RepoFactory = Callable[[str, str], "GitHubRepoLike"]

logger = logging.getLogger(__name__)

# Env var that gates the live-PR path. Default-safe: missing/empty/0 ⇒ refuse.
_LIVE_PR_ENV = "DRIFT_LIVE_PR"
_LIVE_PR_ON = {"1", "true", "yes", "on"}


@dataclass(slots=True, frozen=True)
class PRResult:
    """Outcome of an ``open_pr`` call.

    ``url`` is ``None`` for dry-runs *and* for skipped-because-branch-exists.
    Callers distinguish via ``dry_run`` plus ``skipped_reason``.
    """

    dry_run: bool
    url: str | None
    branch: str
    labels: tuple[str, ...]
    skipped_reason: str | None = None


# ---------------------------------------------------------------------------
# Protocol seam for the live path
# ---------------------------------------------------------------------------
#
# We don't depend on PyGithub directly in the code paths that the unit tests
# exercise. Instead the gateway depends on a tiny ``GitHubRepoLike`` protocol
# that PyGithub's ``Repository`` already satisfies structurally. Tests pass a
# ``FakeGitHubRepo`` to assert behaviour without hitting the network.


class _BranchRef(Protocol):
    @property
    def commit(self) -> Any: ...  # has .sha


class _PullRequest(Protocol):
    html_url: str

    def add_to_labels(self, *labels: str) -> Any: ...


class GitHubRepoLike(Protocol):
    """Subset of ``github.Repository.Repository`` the gateway actually uses."""

    default_branch: str

    def get_branch(self, branch: str) -> _BranchRef: ...
    def create_git_ref(self, ref: str, sha: str) -> Any: ...
    def get_contents(self, path: str, ref: str = ...) -> Any: ...
    def create_file(self, path: str, message: str, content: str, branch: str = ...) -> Any: ...
    def update_file(
        self, path: str, message: str, content: str, sha: str, branch: str = ...
    ) -> Any: ...
    def delete_file(self, path: str, message: str, sha: str, branch: str = ...) -> Any: ...
    def create_pull(
        self, title: str, body: str, head: str, base: str, draft: bool = ...
    ) -> _PullRequest: ...


class GitHubPRGateway:
    """Open (or simulate opening) a pull request for a ``MigrationBundle``.

    Parameters
    ----------
    repo
        Full ``owner/name`` slug, e.g. ``"AntarangSharma/drift-demo-sandbox"``.
    base_branch
        Target branch for the PR. ``None`` means "use ``repo.default_branch``"
        — this matters because old GitHub accounts still default to ``master``.
    console
        Rich ``Console`` used for dry-run rendering. Injectable so tests can
        capture output without monkey-patching.
    token
        GitHub PAT. Only consulted by the live path; ``None`` is fine for
        dry-runs. Live calls without a token raise immediately.
    repo_factory
        Test-seam: a callable ``(slug, token) -> GitHubRepoLike``. Defaults
        to PyGithub. Tests inject a fake to avoid real network calls.
    """

    def __init__(
        self,
        repo: str | None = None,
        *,
        base_branch: str | None = None,
        console: Console | None = None,
        token: str | None = None,
        repo_factory: RepoFactory | None = None,
    ) -> None:
        self.repo_slug = repo
        self.base_branch = base_branch
        self.console = console or Console()
        self._token = token
        self._repo_factory = repo_factory or _pygithub_repo_factory

    # ----------------------------------------------------------------- public

    def open_pr(self, bundle: MigrationBundle, *, dry_run: bool = True) -> PRResult:
        """Open a PR (or print what *would* be opened, when ``dry_run`` is True)."""
        if dry_run:
            self._render_dry_run(bundle)
            return PRResult(
                dry_run=True,
                url=None,
                branch=bundle.branch_name,
                labels=bundle.labels,
            )

        return self._open_live(bundle)

    # -------------------------------------------------------------- live path

    def _open_live(self, bundle: MigrationBundle) -> PRResult:
        if os.environ.get(_LIVE_PR_ENV, "").lower() not in _LIVE_PR_ON:
            raise RuntimeError(
                f"{_LIVE_PR_ENV} is not set. Refusing to open a live PR. "
                f"Re-run with {_LIVE_PR_ENV}=1 to enable."
            )
        if not self.repo_slug:
            raise RuntimeError("GitHubPRGateway.repo is required for live PRs.")
        if not self._token:
            raise RuntimeError(
                "No GitHub token provided. Set DRIFT_GITHUB_TOKEN or pass token=... to the gateway."
            )

        repo = self._repo_factory(self.repo_slug, self._token)
        base = self.base_branch or repo.default_branch

        # Idempotency: never clobber a reviewer's manual edits. If the branch
        # already exists, surface a skip rather than force-pushing.
        if _branch_exists(repo, bundle.branch_name):
            logger.info(
                "drift branch %r already exists on %s — skipping (idempotent)",
                bundle.branch_name,
                self.repo_slug,
            )
            return PRResult(
                dry_run=False,
                url=None,
                branch=bundle.branch_name,
                labels=bundle.labels,
                skipped_reason="branch_exists",
            )

        base_sha = repo.get_branch(base).commit.sha
        repo.create_git_ref(ref=f"refs/heads/{bundle.branch_name}", sha=base_sha)

        commit_msg = f"agent: {bundle.pr_title}"
        for fp in bundle.files:
            _apply_patch(repo, fp, branch=bundle.branch_name, message=commit_msg)

        pr = repo.create_pull(
            title=bundle.pr_title,
            body=bundle.pr_body_markdown,
            head=bundle.branch_name,
            base=base,
            draft=bundle.is_draft,
        )
        if bundle.labels:
            pr.add_to_labels(*bundle.labels)

        logger.info("opened PR %s", pr.html_url)
        return PRResult(
            dry_run=False,
            url=pr.html_url,
            branch=bundle.branch_name,
            labels=bundle.labels,
        )

    # ---------------------------------------------------------------- rendering

    def _render_dry_run(self, bundle: MigrationBundle) -> None:
        c = self.console
        c.rule("[bold cyan]DRY-RUN: Schema-Drift PR")

        c.print(
            Panel.fit(
                f"[bold]repo[/bold]    : {self.repo_slug or '<unset>'}\n"
                f"[bold]base[/bold]    : {self.base_branch or '<repo default>'}\n"
                f"[bold]branch[/bold]  : {bundle.branch_name}\n"
                f"[bold]title[/bold]   : {bundle.pr_title}\n"
                f"[bold]draft[/bold]   : {bundle.is_draft}\n"
                f"[bold]labels[/bold]  : {', '.join(bundle.labels) or '<none>'}\n"
                f"[bold]files[/bold]   : {len(bundle.files)} patch(es)\n"
                f"[bold]llm cost[/bold]: ${bundle.llm_cost_usd:.4f}",
                title="PR metadata",
                border_style="cyan",
            )
        )

        c.rule("[bold]PR body[/bold]")
        c.print(Markdown(bundle.pr_body_markdown))

        c.rule("[bold]File patches[/bold]")
        for fp in bundle.files:
            c.print(
                Panel(
                    fp.content,
                    title=f"[{fp.mode}] {fp.path}",
                    border_style="green" if fp.mode != "delete" else "red",
                )
            )

        if bundle.backfill_sql:
            c.rule("[bold yellow]Backfill SQL")
            c.print(bundle.backfill_sql)
        if bundle.rollback_sql:
            c.rule("[bold yellow]Rollback SQL")
            c.print(bundle.rollback_sql)

        c.rule("[bold cyan]END DRY-RUN")


# ---------------------------------------------------------------------------
# Helpers (module-private; importable for tests)
# ---------------------------------------------------------------------------


def _pygithub_repo_factory(slug: str, token: str) -> GitHubRepoLike:  # pragma: no cover
    """Default factory: real PyGithub ``Repository``.

    Tests inject their own factory to keep the live API out of the unit
    suite; production callers get the real thing. Excluded from coverage
    because exercising it requires real GitHub credentials — that path is
    covered by the opt-in ``tests/test_pr_live.py`` integration test.
    """
    gh = Github(auth=Auth.Token(token))
    return gh.get_repo(slug)  # type: ignore[return-value]


def _branch_exists(repo: GitHubRepoLike, branch: str) -> bool:
    """Return ``True`` iff ``branch`` already exists on the remote.

    PyGithub raises ``UnknownObjectException`` for missing branches; we
    catch that narrowly and let any other ``GithubException`` propagate
    so transport errors don't look like "branch doesn't exist, go ahead".
    """
    try:
        repo.get_branch(branch)
        return True
    except UnknownObjectException:
        return False


def _apply_patch(repo: GitHubRepoLike, fp: FilePatch, *, branch: str, message: str) -> None:
    """Translate a ``FilePatch`` into the right PyGithub call.

    ``create_file`` / ``update_file`` / ``delete_file`` is each 1 API request,
    so a multi-file bundle is N requests + 1 for the branch + 1 for the PR.
    That's fine for the 1-file patches we ship today; multi-file bundles in
    Week 4+ will switch to the Git Data API (single commit via ``create_tree``).
    """
    if fp.mode == "create":
        repo.create_file(path=fp.path, message=message, content=fp.content, branch=branch)
        return

    if fp.mode == "update":
        existing: Any = repo.get_contents(fp.path, ref=branch)
        repo.update_file(
            path=fp.path,
            message=message,
            content=fp.content,
            sha=existing.sha,
            branch=branch,
        )
        return

    if fp.mode == "delete":
        existing = repo.get_contents(fp.path, ref=branch)
        repo.delete_file(path=fp.path, message=message, sha=existing.sha, branch=branch)
        return

    # Pydantic's ``Literal`` validator already rejects anything outside the
    # three modes above; this is belt-and-braces for someone hand-rolling a
    # FilePatch without going through the model. Unreachable in practice.
    raise ValueError(f"Unknown FilePatch mode: {fp.mode!r}")  # pragma: no cover


def labels_for(bundle: MigrationBundle, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Return the dedup'd label set for a bundle plus any extras."""
    seen: dict[str, None] = dict.fromkeys(bundle.labels)
    for lbl in extra:
        seen.setdefault(lbl, None)
    return tuple(seen.keys())


__all__ = ["GitHubPRGateway", "GitHubRepoLike", "PRResult", "labels_for"]
