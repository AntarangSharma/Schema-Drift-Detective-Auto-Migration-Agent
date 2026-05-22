# Weeks 2–8 master plan — closed for v0.8.0

Status as of 2026-05-21: all weeks landed. Single deferred item is
the corpus-level `Mig. ✓` cell; see `docs/06_launch_checklist.md`
§ "Deferred-funded-run".

## Week 2 — Classifier expansion + benchmark moat ✅
- [x] Expand `Classifier` to all 13 `ChangeType`s (`removed`/`modified`/
      table-add/table-remove diff kinds + rename detection across `removed`+
      `added` pairs).
- [x] `bench/generate.py` — synthetic scenario generator: 18 base tables
      (TPC-H + taxi + Stripe-like) × 13 change types × variants ⇒ ≥300
      scenarios (shipped: 364). Deterministic via `--seed 20260101`.
- [x] `bench/runner.py` — `--method {ours,ge,dbt,oneshot}`, writes
      `bench/results/results-{method}.json` + summary table.
- [x] First fill of `bench/results/RESULTS.md` ("Ours (rule-only)" only).
- [x] Tests: classifier per change-type (1 happy + 1 edge each).

## Week 3 — Column-level lineage ✅
- [x] `lineage.from_manifest_with_columns(...)` — walks compiled SQL via
      SQLGlot, builds a `(model, column)` DiGraph.
- [x] Handle CTEs, aliases, simple JOIN, UNION ALL. `SELECT *` ⇒ set
      `fan_out_conservative=True`.
- [x] `impact_columns(source_table, column)` returns `((model, col), ...)`.
- [x] Hypothesis property tests on round-trip lineage.
- [x] `RESULTS.md` impact-recall column updated.

## Week 4 — LLM-drafted migration SQL ✅
- [x] `src/schema_drift/llm.py` — thin adapter, Claude/OpenAI swappable
      via env vars. `MockLLM` used in CI tests.
- [x] `MigrationProposal` Pydantic model with `Literal[col_names]` enforced.
- [x] Versioned prompt template in `prompts/migration_drafter.md`.
- [x] Validation loop: `dbt parse && dbt compile`; retry ≤2× with error
      feedback. (Stubbed via `DbtRunner` Protocol in CI.)
- [x] Cost tracking written to `AuditRecord.payload`.

## Week 5 — Baselines + final benchmark ✅
- [x] `bench/baselines/dbt_tests_baseline.py` — replays drift, runs schema
      tests against post-state.
- [x] `bench/baselines/ge_baseline.py` — Great Expectations harness
      (stubbed when env lacks GE).
- [x] `bench/baselines/one_shot_llm_baseline.py` — single Claude call with
      pre+post pasted in (uses `MockLLM` in CI).
- [x] Held-out split via `sha256(scenario_id) % 10 ∈ {7,8,9}`.
- [x] `RESULTS.md` filled across all methods + 3-para analysis.
      `Mig. ✓` for LLM rows marked `deferred` with footnote (single
      deliberately-deferred item; see `docs/06_launch_checklist.md`).
- [x] Confusion-matrix renderer (no matplotlib in CI — emits text matrix).

## Week 6 — Guardrails, observability, ADRs ✅
- [x] `policy.py` — `PolicyEngine.decide(event, impact) → Action` with
      blast-radius cap, kill-switch env var, rate limit, destructive gate.
- [x] `audit.py` — every step writes an `AuditRecord`.
- [x] `ol.py` — OpenLineage emitter (HTTP POST to Marquez; no-op when
      `OPENLINEAGE_URL` unset).
- [x] `slack.py` — Block-Kit message; no-op without webhook.
- [x] `metrics.py` — Prometheus counters + histograms.
- [x] `docs/04_architecture.md` (ADR-style; 4 decisions).
- [x] `docs/05_benchmark.md`.

## Week 7 — Stretch adapters ✅
- [x] `watcher/duckdb.py` — minimal smoke watcher.
- [x] `watcher/debezium.py` — adapter stub conforming to `SourceWatcher`.
- [x] `watcher/snowflake.py` — stub raising clear "untested" error.
- [x] `bi/metabase.py` — adapter stub, emits empty dashboards by default.
- [x] DuckDB end-to-end test (skip if duckdb not installed).

## Week 8 — Launch polish ✅
- [x] README v2 — hero metric line filled from RESULTS.md + 15s GIF
      placeholder + benchmark table inlined.
- [x] `docs/BLOG.md` — ~1577-word post draft.
- [x] `docs/06_launch_checklist.md` — with explicit deferred-funded-run
      section.
- [x] CHANGELOG consolidated `[0.8.0]` entry for W2–W8.
- [x] `pyproject.toml` `version` bumped to `0.8.0`.
- [ ] Final quality gate green; CI matrix green. *Run immediately
      before tagging `v0.8.0`.*

## Post-launch follow-ups (not blocking v0.8.0)
- [ ] Funded Claude run + `dbt-core` binary in CI image to close the
      `Mig. ✓` deferred cell. (~$30, ~1 afternoon.)
- [ ] Real-OSS held-out slice harvested from dbt-core + Stripe schema
      repos.
- [ ] First-class Snowflake adapter (on top of the existing stub).
- [ ] Debezium sub-second non-polling mode.
- [ ] Real OpenLineage `parentRunFacet` schema.
- [ ] Record the 15s demo GIF against the live sandbox repo.
