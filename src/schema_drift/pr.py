"""GitHub PR Gateway.

Day-3 scope
-----------
Only the **dry-run** path is wired: ``open_pr(bundle, dry_run=True)`` writes
the PR body, branch name, labels, and patched files to stdout/a console
renderer. The real GitHub-side wiring (branch creation, file commits,
``gh.create_pull``, ``add_to_labels``) is implemented in Week 1 day 5 once
the demo is reviewer-ready.

Why this is a thin shim
-----------------------
PR mechanics are *the* place where bugs become public. Until the demo is
clean end-to-end I'd rather print than push.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from schema_drift.models import MigrationBundle

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(slots=True, frozen=True)
class PRResult:
    """Outcome of an ``open_pr`` call.

    For dry-runs ``url`` is ``None`` and ``dry_run`` is ``True``.
    """

    dry_run: bool
    url: str | None
    branch: str
    labels: tuple[str, ...]


class GitHubPRGateway:
    """Open (or simulate opening) a pull request for a ``MigrationBundle``.

    Parameters
    ----------
    repo
        Full ``owner/name`` slug, e.g. ``"AntarangSharma/Schema-Drift-Detective"``.
    base_branch
        Target branch for the PR. Defaults to ``"main"``.
    console
        Rich ``Console`` used for dry-run rendering. Injectable so tests can
        capture output without monkey-patching.
    token
        GitHub PAT. Only consulted by the live path (Week 1 day 5); ``None``
        is fine for dry-runs.
    """

    def __init__(
        self,
        repo: str | None = None,
        *,
        base_branch: str = "main",
        console: Console | None = None,
        token: str | None = None,
    ) -> None:
        self.repo = repo
        self.base_branch = base_branch
        self.console = console or Console()
        self._token = token

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

        # Live path is intentionally not implemented yet — fail loud, not silent.
        raise NotImplementedError(
            "Live PR creation lands in Week 1 day 5. Re-run with dry_run=True."
        )

    # ---------------------------------------------------------------- rendering

    def _render_dry_run(self, bundle: MigrationBundle) -> None:
        c = self.console
        c.rule("[bold cyan]DRY-RUN: Schema-Drift PR")

        c.print(
            Panel.fit(
                f"[bold]repo[/bold]    : {self.repo or '<unset>'}\n"
                f"[bold]base[/bold]    : {self.base_branch}\n"
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


def labels_for(bundle: MigrationBundle, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Return the dedup'd label set for a bundle plus any extras."""
    seen: dict[str, None] = dict.fromkeys(bundle.labels)
    for lbl in extra:
        seen.setdefault(lbl, None)
    return tuple(seen.keys())


__all__ = ["GitHubPRGateway", "PRResult", "labels_for"]
