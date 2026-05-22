# What's Left to Build

> Snapshot date: **2026-05-21**.
> Excludes anything purely test-related (CI matrix, coverage chase, etc.) —
> focuses only on **shippable code + docs artifacts** that are still missing
> from the v2 build plan (`tasks/todo.md`).

Cross-referenced against the current repo state. Modules that already exist
on disk (`policy.py`, `audit.py`, `ol.py`, `slack.py`, `metrics.py`,
`watcher/{duckdb,debezium,snowflake}.py`, `bi/metabase.py`, the 364 bench
scenarios, all 4 baselines, the 5 ADR docs, etc.) are **not** repeated here.

---

## 🔴 P0 — Blocks a "feature-complete v0.8" tag

### 1. Real `DbtRunner` implementation (W4 carry-over)
- `src/schema_drift/llm.py` still ships only `StubDbtRunner` ("always
  succeeds"). The inline comment literally says *"Real impl arrives
  Week 5"* — it never landed.
- Need a `SubprocessDbtRunner` that:
  - Shells out to `dbt parse && dbt compile` against the patched
    `dbt_project/` working tree.
  - Captures stderr → returns `(ok: bool, err: str)` matching the
    Protocol.
  - Honors `DRIFT_DBT_PROJECT_DIR` / `DRIFT_DBT_PROFILES_DIR` env vars.
  - Has a `timeout_seconds` knob (default 60 s) so a stuck dbt won't
    hang the agent.
- Wire it as the default in `MigrationDrafter` / the live PR path.

### 2. Migration-correctness column in `RESULTS.md` (W5 carry-over)
- `bench/results/RESULTS.md` still has `_` in the **Mig. ✓** column for
  *every* method except the rule-only baseline.
- Need:
  - `bench/runner.py --method ours+llm` path that actually runs
    `MockLLM` end-to-end and scores `compiles & dbt-tests-pass`.
  - Re-emit `results.json` and refresh the markdown table.
  - Same for the one-shot LLM baseline.

### 3. README v2 — hero block (W8)
- `README.md` still reads `Hero metrics _(to be filled in by Week 5)_`
  with `__.__%` placeholders. Replace with the real numbers from
  `RESULTS.md` (Drift R 1.00, Class. macro-F1 1.00, Impact R 1.0,
  Latency 12 ms, $/1k $0.10).
- Add the 4-row benchmark table (Ours vs GE vs dbt-tests vs one-shot
  LLM) directly under the hero block.
- Add the **15-second GIF placeholder** — referenced in `tasks/todo.md`
  W8 but no `docs/figs/demo.gif` (or `.svg`) exists yet.

### 4. Version bump to `0.8.0`
- `pyproject.toml` is still on `version = "0.1.0"`.
- Plan calls for "one minor per week" → at end of W8 we should be at
  `0.8.0`. Bump in `pyproject.toml`, regenerate any cached metadata.

---

## 🟠 P1 — Launch-polish docs that don't exist yet

### 5. `docs/BLOG.md` (W8)
- ~1500-word launch post draft. Plan outline:
  1. The 3 am page (problem framing).
  2. Why "deterministic detector + LLM drafter" beats either alone.
  3. Benchmark headline + held-out methodology.
  4. Live PR walkthrough (link to sandbox PR).
  5. What's next (Snowflake adapter, Debezium watcher, multi-warehouse).
- Reuse phrasing from `docs/02_revised_plan.md` and the `RESULTS.md`
  analysis paragraphs — don't re-derive.

### 6. `docs/06_launch_checklist.md` (W8)
- Pre-flight checklist for the public launch:
  - [ ] PyPI release tag.
  - [ ] GitHub release notes pulled from CHANGELOG.
  - [ ] Sandbox PR pinned in README.
  - [ ] Show HN / r/dataengineering post drafts.
  - [ ] Docs site (if any) deployed.
  - [ ] Telemetry / metrics endpoints sanity-checked.

### 7. CHANGELOG entries for W2 → W8 (W8)
- `CHANGELOG.md` currently jumps from Phase 0 → Day 3/4/5 only. Missing
  the seven week-level entries promised by the plan. Each week needs a
  one-paragraph "Added / Changed / Safety" block stitched from the
  module-level docstrings + commits.

---

## 🟡 P2 — Stretch / nice-to-haves

### 8. Real-world OSS manifest re-measurement (W8 stretch)
- `RESULTS.md` footnote ²: *"Real-OSS manifests in Week 8 will re-measure
  on a larger corpus."* — not done yet. Pull 1–2 public dbt projects
  (e.g. `dbt-labs/jaffle_shop`), regenerate scenarios against their
  `manifest.json`, update the benchmark.

### 9. Funded-LLM cost numbers (W5 footnote)
- `RESULTS.md` footnote ¹: *"Real $ figures land once the funded Claude
  path is wired in."* — currently projected from `MockLLM` token counts.
  Needs a one-shot run with a real API key + cost log.

### 10. Snowflake watcher — beyond the "untested" stub
- `src/schema_drift/watcher/snowflake.py` is 66 lines and per the plan
  raises a clear *"untested"* error. Acceptable for v0.8 but should be
  promoted to a real implementation before a 1.0 cut. Out of scope for
  the current launch.

### 11. Metabase BI adapter — beyond "emits empty dashboards"
- `bi/metabase.py` exists (218 lines) but the plan says it "emits empty
  dashboards by default." A real wire-up against a live Metabase
  instance is still missing.

---

## ✅ Already done (so we don't relitigate it)

- Weeks 2–7 in `tasks/todo.md` are essentially complete on disk:
  - **W2**: Classifier covers all 13 `ChangeType`s; 364 generated scenarios.
  - **W3**: Column-level lineage in `lineage.py` (540 LOC).
  - **W4**: `llm.py` + `MigrationProposal` + `prompts/migration_drafter.md`
    + cost tracking — only the *real* `DbtRunner` is missing (see P0 #1).
  - **W5**: All 3 baselines + `bench/confusion.py` + held-out split + 3-para
    analysis in `RESULTS.md`.
  - **W6**: `policy.py`, `audit.py`, `ol.py`, `slack.py`, `metrics.py`,
    `docs/04_architecture.md`, `docs/05_benchmark.md`.
  - **W7**: DuckDB / Debezium / Snowflake watchers, Metabase BI adapter.

---

## Suggested order of operations

1. **P0 #1** (real `DbtRunner`) — unblocks P0 #2.
2. **P0 #2** (fill `Mig. ✓` cells) — unblocks P0 #3.
3. **P0 #3** (README hero + GIF) — public face.
4. **P0 #4** (version bump) — ties release.
5. **P1 #5 + #6 + #7** in parallel — pure docs.
6. **P2** items only if the launch isn't time-boxed.

Estimated effort to clear P0 + P1: **~2 focused days** of work given the
underlying modules already exist.
