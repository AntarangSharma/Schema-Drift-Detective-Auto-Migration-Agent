# Changelog

All notable changes to Schema Drift Detective. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html), though the
0.x line is pre-release and breaking changes can land on any commit.

## [Unreleased]

### Added — Day 5 (2026-05-21)
- Live `GitHubPRGateway.open_pr` against [`drift-demo-sandbox`](https://github.com/AntarangSharma/drift-demo-sandbox)
  using PyGithub 2.x. Resolves `repo.default_branch`, cuts a `drift/<ulid>`
  branch, applies each `FilePatch` via `create_file` / `update_file` /
  `delete_file`, opens a draft PR, applies labels.
- `GitHubRepoLike` Protocol seam — tests inject `FakeGitHubRepo`, production
  uses PyGithub via `_pygithub_repo_factory`.
- `make demo-live` target — opens a real PR end-to-end. Refuses without
  `DRIFT_LIVE_PR=1` *and* `DRIFT_GITHUB_TOKEN` (belt-and-braces with the
  in-process gate).
- `tests/test_pr_live.py` — opt-in `@pytest.mark.live` integration test
  with belt-and-braces env-var skipif. Default `pytest -m "not live"` skips.
- `docs/03_live_pr_path.md` — post-mortem covering the 9-step live flow
  and the partial-state failure modes when PyGithub throws mid-bundle.
- README callout pointing readers at the sandbox PR feed.

### Changed — Day 5
- `pr.py` `PRResult` gains `skipped_reason: str | None` so callers can
  distinguish "live open succeeded" from "branch already existed, skipped".
- `demo.py` reads `DRIFT_GITHUB_TOKEN` from env and prints the opened-PR
  URL as a Rich hyperlink.
- `Makefile` `test-fast` / `test` now skip the `live` marker by default.

### Safety / hardening — Day 5
- **Idempotent**: if `bundle.branch_name` already exists on the remote,
  `open_pr` returns a `PRResult` with `skipped_reason="branch_exists"`
  and writes nothing. The agent will never force-push over a reviewer's
  manual edits.
- **Explicit opt-in**: live path raises `RuntimeError` unless the
  `DRIFT_LIVE_PR` env var is set to `1`/`true`/`yes`/`on`.

### Added — Day 4 (2026-05-21)
- `storage/snapshot_store.py` — `SnapshotStore` Protocol plus
  `PostgresSnapshotStore` (backed by `schema_drift.schema_snapshots`) and
  `InMemorySnapshotStore` (tests + cold-start fallback). Wire format is
  `SchemaSnapshot.model_dump(mode="json")` round-tripped through
  `model_validate`.
- `runner.py` — `WatcherRunner.run_once() → RunResult(snapshot, events, is_baseline)`.
  First run for a `source_identifier` surfaces `is_baseline=True` instead
  of being conflated with "no drift".
- `drift watch --once` CLI subcommand, Postgres-backed by default with
  `--dsn` / `--schemas` / `--source-identifier` overrides.
- `tests/test_postgres_integration.py` — opt-in `@pytest.mark.integration`
  tests against the docker-compose Postgres. Auto-skip when unreachable
  so the unit suite stays hermetic.

### Changed — Day 4
- `docker-compose.yml` host port `5432 → 55432`. macOS / Linux developers
  with a system Postgres install no longer collide.
- Default DSN everywhere bumped to `postgresql://drift:drift@localhost:55432/drift`.

### Added — Day 3 (initial thin slice)
- End-to-end pipeline: `SourceWatcher → Classifier → LineageGraph →
  MigrationDrafter → GitHubPRGateway` (dry-run only at this stage).
- Classifier covers 3 `COLUMN_ADDED_*` variants; rest raise typed errors.
- `MigrationDrafter` patches `dbt_project/models/sources.yml` via
  ruamel.yaml round-trip (preserves comments + key order + indentation).
- `LineageGraph.from_manifest()` walks `dbt manifest.json` to compute
  downstream impact; falls back to a hand-rolled `ImpactSet` when no
  manifest is on disk.
- `drift demo --dry-run` CLI subcommand renders a simulated PR.

### Project foundations (Phase 0)
- Pydantic 2.x contracts for `RawChange` / `DriftEvent` / `ImpactSet` /
  `MigrationBundle` / `SchemaSnapshot` etc. All frozen (`extra="forbid"`,
  `str_strip_whitespace=True`).
- CI matrix: Python 3.12 + 3.13, ruff + ruff-format + pyright + pytest
  with coverage. Strict markers + strict config.
- Docker Compose for local Postgres (init SQL + seed data baked in).
- Project docs: initial spec, v2 revised plan, ADRs.

---

[Unreleased]: https://github.com/AntarangSharma/Schema-Drift-Detective-Auto-Migration-Agent/commits/main
