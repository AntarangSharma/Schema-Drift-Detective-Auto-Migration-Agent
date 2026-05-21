# 03 — Live PR Path (Day 5 post-mortem)

> What happens between `drift watch --once` detecting a column add and a real
> draft PR appearing on `drift-demo-sandbox/pulls`. Written immediately after
> Day 5 shipped (commit `923fda5`) so the failure modes are still fresh.

## The path, in 9 steps

1. `WatcherRunner.run_once()` returns `RunResult(events=[DriftEvent(...)], is_baseline=False)`.
   The event carries `change_type=COLUMN_ADDED_NULLABLE`, severity `low`, and
   the resolved `ImpactSet` (downstream dbt models).
2. `MigrationDrafter.draft(event, impact)` round-trips `dbt_project/models/sources.yml`
   through ruamel.yaml, inserts the new column under the right table, and
   produces a `MigrationBundle` with one `FilePatch` (mode=`update`) and a
   markdown PR body rendered from a Jinja template.
3. `GitHubPRGateway.open_pr(bundle, dry_run=False)` is called from `demo.py`
   with `repo=os.getenv("DRIFT_GITHUB_REPO")` and `token=os.getenv("DRIFT_GITHUB_TOKEN")`.
4. The gateway checks `os.environ["DRIFT_LIVE_PR"] in {"1","true","yes","on"}`.
   Missing/empty/`0` ⇒ raise `RuntimeError`. No request leaves the process.
5. The gateway also checks `repo` and `token` are set. Either missing ⇒ raise.
6. `_pygithub_repo_factory(slug, token)` instantiates
   `Github(auth=Auth.Token(token))` and resolves the slug. This is the only
   place we touch PyGithub directly; everything downstream works against the
   `GitHubRepoLike` Protocol so unit tests inject a `FakeGitHubRepo`.
7. `_branch_exists(repo, bundle.branch_name)` — catches PyGithub's
   `UnknownObjectException` narrowly. If the branch already exists we
   short-circuit with `PRResult(skipped_reason="branch_exists")` and write
   nothing. **No force-push, ever.**
8. Else: `repo.get_branch(repo.default_branch).commit.sha` → `create_git_ref`
   → per-`FilePatch` `create_file` / `update_file` / `delete_file` →
   `repo.create_pull(...draft=bundle.is_draft)` → `pr.add_to_labels(...)`.
9. Return `PRResult(dry_run=False, url=pr.html_url, ...)`. `demo.py` prints
   it as a Rich hyperlink so the URL is one click away in any modern terminal.

## Two-layer safety

The `DRIFT_LIVE_PR=1` gate lives in two places on purpose:

| Layer | Check | Why |
|---|---|---|
| `make demo-live` | bash `if [ "$$DRIFT_LIVE_PR" != "1" ]` | catches `make demo-live` typos before Python ever boots |
| `GitHubPRGateway._open_live` | `os.environ.get(...)` | catches every other entry point (CLI, library use, tests) |

Belt-and-braces. If you delete one, the other still refuses to open a PR.

## What happens if PyGithub throws mid-flow?

This was the question I kept coming back to. Let me trace each failure mode:

| Failure point | What's already happened | What hasn't | Net effect |
|---|---|---|---|
| `get_branch(default_branch)` fails | nothing | everything | clean — no remote state changed |
| `create_git_ref` fails | nothing | everything | clean — branch never came into existence |
| `update_file` fails on file 1 of N | branch exists, no commits | files 2..N + PR + labels | **partial state**: empty branch left behind |
| `update_file` fails on file K of N | branch exists, K-1 commits | files K..N + PR + labels | **partial state**: branch with partial patches, no PR |
| `create_pull` fails | branch + all commits | the PR + labels | **partial state**: branch with all patches, no PR |
| `add_to_labels` fails | branch + commits + PR | just the labels | **partial state**: unlabelled PR |

The first two failure modes are clean. The last four leave the remote in a
state where the *next* run will hit the branch-exists idempotency check and
skip — which is the right default if the agent is wedged.

**What would actually fix this:** swap the multi-call sequence for the Git
Data API (`create_tree` + `create_commit` + `create_ref`), so a multi-file
bundle is one transactional commit. The trade-off is more code complexity
for a Day-5 prototype where bundles are always 1 file. Filed as a Week-4
chore (multi-file bundles arrive there with the LLM-drafted migrations).

In the meantime, the partial-state branches need manual cleanup or a
`drift janitor` command. Adding one when we see it actually happen.

## What I'd do differently next time

- **Protocol seam from day one.** I almost wrote the live path against
  `Github` directly and only carved out `GitHubRepoLike` when test discovery
  led me there. Having the Protocol first would have saved a refactor.
- **Test the failure modes, not just the success.** The `FakeGitHubRepo` makes
  it cheap to assert "if `update_file` raises, no PR is opened". I haven't
  added those tests yet — they belong in Week 4 when we have multi-file
  bundles and the partial-state problem actually bites.
- **`pragma: no cover` on the production factory.** The only line of `pr.py`
  that touches the real GitHub API can't be exercised in CI without a token,
  and pretending otherwise produces fake coverage numbers. Marking it
  explicitly is honest.

## Recipe to re-validate after a PyGithub upgrade

```bash
DRIFT_LIVE_PR=1 \
DRIFT_GITHUB_TOKEN=ghp_xxx \
DRIFT_GITHUB_REPO=AntarangSharma/drift-demo-sandbox \
.venv/bin/pytest -m live tests/test_pr_live.py -v
```

Opens one real PR, asserts on the URL shape, exits 0. Close the PR manually.
If PyGithub changes its `Auth` shape (it did between 1.x and 2.x), this is
the test that screams loudest.
