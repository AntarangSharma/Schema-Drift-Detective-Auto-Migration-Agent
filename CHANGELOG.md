# Changelog

All notable changes to Schema Drift Detective. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html), though the
0.x line is pre-release and breaking changes can land on any commit.

## [0.8.0] — 2026-05-21

This release consolidates Weeks 2–8 of the v2 build plan into a single
launch tag. See `docs/02_revised_plan.md` for the design, `docs/BLOG.md`
for the launch post, and `docs/06_launch_checklist.md` for the one
deliberately deferred item.

### Added — Week 8 (launch polish)
- `docs/BLOG.md` — ~1500-word launch post draft.
- `docs/06_launch_checklist.md` — final shippable checklist, including
  the single `deferred-funded-run` item (`Mig. ✓` corpus number).
- README v2 — hero metrics filled from `bench/results/RESULTS.md`,
  benchmark table inlined, 15s GIF placeholder, `Phase 0 scaffold`
  banner replaced with v0.8.0 status.
- `pyproject.toml` `version` bumped `0.1.0 → 0.8.0`.

### Added — Week 7 (stretch adapters)
- `watcher/duckdb.py` — minimal smoke watcher.
- `watcher/debezium.py` — adapter stub conforming to `SourceWatcher`.
- `watcher/snowflake.py` — stub raising a typed
  `NotImplementedError("untested")`.
- `bi/metabase.py` — adapter stub, emits empty dashboards by default.
- `tests/test_duckdb_e2e.py` — end-to-end DuckDB test, auto-skipped
  when `duckdb` not installed.

### Added — Week 6 (guardrails, observability, ADRs)
- `policy.py` — `PolicyEngine.decide(event, impact) → Action` with
  blast-radius cap, kill-switch env var, rate limit, destructive gate.
- `audit.py` — `AuditRecord` writer for every pipeline step.
- `ol.py` — OpenLineage emitter (HTTP POST to Marquez; no-op when
  `OPENLINEAGE_URL` unset).
- `slack.py` — Block-Kit message emitter; no-op without webhook URL.
- `metrics.py` — Prometheus counters + histograms.
- `docs/04_architecture.md` — ADR-style, 4 decisions.
- `docs/05_benchmark.md` — benchmark design notes.

### Added — Week 5 (baselines + final benchmark)
- `bench/baselines/dbt_tests_baseline.py` — replays drift, runs schema
  tests against the post-state snapshot.
- `bench/baselines/ge_baseline.py` — Great Expectations harness;
  auto-stubs out when GE isn't on the env.
- `bench/baselines/one_shot_llm_baseline.py` — single Claude call with
  pre+post snapshots pasted in; `MockLLM` in CI.
- `bench/all_methods.py` — runs all four methods, writes
  `bench/results/results-*.json` and confusion matrices.
- `bench/confusion.py` — plain-text confusion-matrix renderer
  (matplotlib-free in CI).
- Held-out split via `sha256(scenario_id) % 10 ∈ {7, 8, 9}`.
- `bench/results/RESULTS.md` filled across all methods with a 3-para
  analysis. `Mig. ✓` cells for LLM rows explicitly marked `deferred`
  with footnote pointing at `docs/06_launch_checklist.md`.

### Added — Week 4 (LLM-drafted migration SQL)
- `src/schema_drift/llm.py` — thin Claude/OpenAI adapter, swappable
  via env vars. `MockLLM` used in CI.
- `MigrationProposal` Pydantic model with `Literal[col_names]`
  enforced — model cannot hallucinate a non-existent column.
- `prompts/migration_drafter.md` — versioned prompt template.
- Validation loop: `dbt parse && dbt compile`, retry ≤2× with error
  feedback. `DbtRunner` Protocol stubbed in CI.
- Cost tracking written to `AuditRecord.payload`.
- `tests/test_llm.py` — full retry-on-compile-failure coverage.

### Added — Week 3 (column-level lineage)
- `lineage.from_manifest_with_columns(...)` — walks compiled SQL via
  SQLGlot, builds a `(model, column)` DiGraph.
- Handles CTEs, aliases, simple `JOIN`, `UNION ALL`. `SELECT *` sets
  `fan_out_conservative=True` and marks every downstream column
  potentially-affected.
- `impact_columns(source_table, column)` returns `((model, col), ...)`.
- Hypothesis property tests on round-trip lineage.

### Added — Week 2 (classifier expansion + benchmark moat)
- `Classifier` expanded to all 13 `ChangeType`s — `removed` / `modified`
  / table-add / table-remove diff kinds + rename detection across
  `removed`+`added` pairs.
- `bench/generate.py` — synthetic scenario generator. 18 base tables
  (TPC-H + NYC taxi + Stripe-like) × 13 change types × 2 variants
  ⇒ **364 scenarios**, deterministic via `--seed 20260101`.
- `bench/runner.py` — `--method {ours,ge,dbt,oneshot}`, writes
  `bench/results/results-{method}.json`.
- First fill of `bench/results/RESULTS.md` (rule-only column).
- Per-change-type classifier tests (1 happy + 1 edge each).

### Deferred (tracked in `docs/06_launch_checklist.md`)
- Corpus-level `Mig. ✓` (migration correctness over the 110 held-out
  scenarios) for both LLM methods. Mechanism shipped + integration-
  tested; corpus aggregate gated on a funded Claude run + `dbt-core`
  binary in CI. Estimated cost: ~$30, ~1 afternoon of work.

---

## [Unreleased — pre-0.8.0]

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

[0.8.0]: https://github.com/AntarangSharma/Schema-Drift-Detective-Auto-Migration-Agent/releases/tag/v0.8.0
[Unreleased — pre-0.8.0]: https://github.com/AntarangSharma/Schema-Drift-Detective-Auto-Migration-Agent/commits/main
