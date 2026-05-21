# Week 1, Days 4-5 — "Make it real"

**Goal:** turn the Day-3 thin slice into a live system. By end of Day 5 a real
PR is opened against `drift-demo-sandbox` by the agent, after a real Postgres
`ALTER TABLE` is detected by the polling watcher.

**Acceptance criterion:** a PR visible at
`github.com/AntarangSharma/drift-demo-sandbox/pulls`, opened by the agent,
patching `dbt_project/models/sources.yml`, labelled `[schema-drift, severity:low]`,
on a branch named `drift/<ulid>`.

---

## Decisions baked in (override before I start if you disagree)

- [ASSUMPTION] **Sandbox repo**: new `drift-demo-sandbox` repo, public, MIT,
  contains *only* the dbt project + seed SQL. Lets recruiters see agent-opened
  PRs without dev clutter.
- [ASSUMPTION] **Safety gate**: live PR path requires `DRIFT_LIVE_PR=1`.
  Default behaviour everywhere else (incl. CI) is dry-run.
- [ASSUMPTION] **Auth**: fine-grained PAT in `DRIFT_GITHUB_TOKEN`, scoped to
  contents:write + pull-requests:write on the sandbox repo only.
- [ASSUMPTION] **Snapshot storage**: JSON column in
  `schema_drift.schema_snapshots`, one row per `(source_identifier, captured_at)`,
  no historical retention policy yet (Week 6).

## Out of scope for Days 4-5 (deliberate)

- Expanding the classifier beyond the 3 added-column variants (Day 6).
- Column-level lineage via SQLGlot (Week 3).
- LLM-drafted migrations (Week 4).
- Multi-source polling / scheduling daemon (Week 6).

---

## Day 4 — Persistence + watcher loop ✅ DONE 2026-05-21

- [x] **DDL touch-up** — schema already correct in `infra/postgres-init.sql`
      (no migration required).
- [x] **`storage/snapshot_store.py`** — `SnapshotStore` Protocol plus
      `PostgresSnapshotStore` and `InMemorySnapshotStore` impls. Wire format
      is `SchemaSnapshot.model_dump(mode="json")` round-tripped through
      `model_validate`.
- [x] **`runner.py`** — `WatcherRunner.run_once() → RunResult(snapshot, events,
      is_baseline)`. Pure orchestration; first call surfaces
      `is_baseline=True` instead of being conflated with "no drift".
- [x] **`cli.py watch --once`** — Postgres-backed by default; options for
      `--dsn / --schemas / --source-identifier`. First run exits 0 with a
      "baseline captured" line (per pre-implementation decision).
- [x] **Tests** (15 new cases)
  - `tests/test_snapshot_store.py` — in-memory store contract + JSON
    round-trip.
  - `tests/test_runner.py` — `_ScriptedWatcher` + `InMemorySnapshotStore`
    exercise baseline / no-change / nullable-add / persistence / classifier
    returning None.
  - `tests/test_cli_watch.py` — guidance on missing `--once`, help listing.
  - `tests/test_postgres_integration.py` — opt-in live tests against
    docker-compose (auto-skip if unreachable).
- [x] **Bonus**: docker-compose host port moved to `55432` to dodge laptop
      Postgres installs.
- [x] Quality gate: ruff ✓ format ✓ pyright ✓ pytest ✓ (86 tests, 92% cov).

**Day 4 acceptance verified locally** (live Postgres on `55432`):

    drift watch --once → ✓ baseline snapshot captured
    drift watch --once → ✓ no drift detected
    psql ALTER TABLE source_raw.orders ADD COLUMN discount_code TEXT;
    drift watch --once → ⚠ 1 drift event(s) detected
                          • column_added_nullable → source_raw.orders.discount_code

## Day 5 — Live PR opening ✅ CODE DONE 2026-05-21 (live acceptance pending user PAT)

- [x] **Create `drift-demo-sandbox` repo** (public, MIT, autoInit) —
      https://github.com/AntarangSharma/drift-demo-sandbox.
  - [x] Pushed `dbt_project/` into the sandbox (sources.yml + staging +
        marts + profiles).
  - [x] Sandbox README explains "PRs here are all opened by the agent".
- [x] **Live `pr.py`** — full PyGithub 2.x implementation:
  - [x] Resolves `base_sha` via `repo.get_branch(repo.default_branch).commit.sha`
        (never hardcodes `main` — old GH accounts still default to `master`).
  - [x] Creates branch via `repo.create_git_ref("refs/heads/<branch>", sha)`.
  - [x] Routes each `FilePatch` through `create_file` / `update_file` /
        `delete_file` per its `mode`.
  - [x] `repo.create_pull(title, body, head, base, draft=bundle.is_draft)`.
  - [x] `pr.add_to_labels(*bundle.labels)` (only if labels non-empty).
  - [x] Returns `PRResult(dry_run=False, url=pr.html_url, branch=..., labels=...)`.
- [x] **Safety**: live path raises `RuntimeError` if `DRIFT_LIVE_PR != 1`,
      `repo` is None, or token is None. Verified by unit tests.
- [x] **Idempotency**: branch-exists is detected via PyGithub's
      `UnknownObjectException` and surfaces as
      `PRResult(skipped_reason="branch_exists", url=None)`. No force-push.
- [x] **`make demo-live`** — gates on `$DRIFT_LIVE_PR=1` + non-empty
      `$DRIFT_GITHUB_TOKEN`, exits 2 otherwise. Sets
      `DRIFT_GITHUB_REPO=AntarangSharma/drift-demo-sandbox` by default.
- [x] **Tests** (10 new cases):
  - `tests/test_pr.py` — `FakeGitHubRepo` (satisfies the `GitHubRepoLike`
    Protocol) covering: missing env var, missing token, missing repo,
    happy update, create mode, delete mode, default-branch resolution
    (`master`), explicit base-branch override, no-labels short-circuit,
    branch-exists idempotency. Plus dry-run backfill/rollback rendering.
  - `tests/test_pr_live.py` — opt-in test gated by
    `@pytest.mark.live` *and* env-var skipif. Default `pytest -m "not live"`
    skips it; CI is unaffected.
- [x] Quality gate: ruff ✓ format ✓ pyright ✓ pytest ✓ (96 passed, 1
      skipped (live), 92% cov).

**Day 5 acceptance** (run locally to verify):

    DRIFT_LIVE_PR=1 \
    DRIFT_GITHUB_TOKEN=ghp_xxx \
    make demo-live

…opens a real PR at https://github.com/AntarangSharma/drift-demo-sandbox/pulls.

## Parallel small items (15 min each, do whenever)

- [ ] README: add the `DRIFT_LIVE_PR` callout + sandbox repo link.
- [ ] `CHANGELOG.md` with Day-3 and Day-4/5 entries.
- [ ] Bump coverage on `pr.py` (77% → 90%) and `migrator.py` (92% → 95%) —
      cover the rollback/backfill rendering branches.
- [ ] Add a `docs/03_live_pr_path.md` short post-mortem of the day-5 work
      (interview talking point: "what's the failure mode if PyGithub throws
      mid-PR creation?").

## Risks / things that bite

1. **GitHub API rate limit on file creation** — `create_file` is 1 req per
   file. For multi-file bundles (Week 4+) we'll need the Git Data API
   (`create_tree` + single commit). For now: 1-file patches only.
2. **PyGithub 2.x has different `Auth` shape** — `Github(auth=Auth.Token(...))`
   not `Github(token)`. Get this wrong and the agent silently uses anonymous.
3. **Sandbox repo's default branch on auto-init is `main`** — but old GH
   accounts default to `master`. Need to read `repo.default_branch` not assume.
4. **dbt manifest in sandbox repo** — sandbox has `sources.yml` but no
   compiled `manifest.json`. Options: (a) check in a pre-compiled manifest,
   (b) run `dbt parse` in CI before the demo, (c) keep the day-3 manifest
   fixture as the source of truth for lineage. I'll pick (a) for Day 5
   simplicity; (b) is a Week 2 cleanup.
