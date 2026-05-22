# Launch checklist — v0.8.0

> Status as of 2026-05-21. Everything below is either ✅ done,
> 🟡 deliberately deferred (with a documented reason), or 🔴 blocking.
> If anything is 🔴 at launch time, do not ship.

## Code & functionality

- [x] All 13 `ChangeType`s classified deterministically (`classifier.py`).
- [x] Column-level lineage walk via SQLGlot, with `SELECT *` fan-out
      conservative flag (`lineage.py`).
- [x] 364-scenario synthetic benchmark corpus, deterministic via
      `--seed`. 110 held-out via sha256 split.
- [x] Four methods scored on held-out: GE, dbt tests, one-shot LLM,
      ours. Confusion matrices rendered to plain text (no matplotlib
      in CI).
- [x] LLM drafter with `Literal[col_names]` enforced + retry-on-
      compile-error loop. `MockLLM` used in CI.
- [x] Policy engine: blast-radius cap, kill-switch env var, rate
      limiting, destructive-change gate.
- [x] Audit log: every step writes an `AuditRecord`.
- [x] OpenLineage emitter (no-op when `OPENLINEAGE_URL` unset).
- [x] Slack Block-Kit emitter (no-op without webhook).
- [x] Prometheus metrics (counters + histograms).
- [x] Postgres + DuckDB watchers. Debezium + Snowflake stubs that
      raise typed `NotImplementedError`.
- [x] Metabase BI adapter stub.
- [x] Live `GitHubPRGateway.open_pr` against
      [`drift-demo-sandbox`](https://github.com/AntarangSharma/drift-demo-sandbox/pulls).
- [x] `drift watch --once` and `drift demo --dry-run` CLI subcommands.

## Docs

- [x] `docs/01_initial_spec.md`
- [x] `docs/02_revised_plan.md`
- [x] `docs/03_live_pr_path.md` — post-mortem of the live PR path.
- [x] `docs/04_architecture.md` — 4 ADRs.
- [x] `docs/05_benchmark.md` — design notes on the benchmark.
- [x] `docs/06_launch_checklist.md` (this file).
- [x] `docs/BLOG.md` — ~1500-word launch post draft.
- [x] README v2 — hero metrics filled from RESULTS.md, benchmark table
      pasted, 15s GIF placeholder.

## CI / release

- [x] CHANGELOG entry for the v0.8.0 consolidated release (Weeks 2–8).
- [x] `pyproject.toml` bumped to `0.8.0`.
- [x] `tasks/todo.md` checkboxes flipped to reflect reality.
- [ ] **Final green CI matrix** (Python 3.12 + 3.13, ruff +
      ruff-format + pyright + pytest with coverage, all live markers
      skipped). Run this *once* immediately before tagging.
- [ ] Tag `v0.8.0` and push.

## Deferred-funded-run

> The single thing that is deliberately not measured for v0.8.0.

**Item**: corpus-level `Mig. ✓` (migration correctness — `dbt parse &&
dbt compile` passes) over the 110-scenario held-out split, for both
the **One-shot LLM baseline** and the **Ours (rule + LLM)** method.

**Status**: marked `deferred` in `bench/results/RESULTS.md` and in
the README hero table.

**Why deferred**:

1. Measuring this requires a funded Claude run (estimated $15–30 of
   tokens at current pricing, 110 scenarios × ~2 retries × ~1k input
   tokens × Claude 3.5 Sonnet rates).
2. It also requires a real `dbt-core` binary in the CI image. The
   current CI image is intentionally `dbt`-free to keep cold-start
   under 90 seconds.
3. CI currently runs `MockLLM`. Publishing a `Mig. ✓` number derived
   from `MockLLM` output would be misleading — the mock doesn't
   produce real SQL.
4. The **mechanism** is fully shipped and integration-tested
   (`tests/test_llm.py` exercises the retry-on-compile-failure loop
   against a stubbed `DbtRunner`). What's missing is the
   *corpus-level* aggregate, not the capability.

**How to unblock**:

1. Set `ANTHROPIC_API_KEY` in CI secrets (or a dedicated runner with
   a budget cap).
2. Add `dbt-core==1.8.*` + the configured adapter to the CI image
   (adds ~30s to cold-start; acceptable for the bench job, not for
   the unit-test job).
3. Add `bench/all_methods.py --provider claude --include-compile`
   flag.
4. Run against the 110 held-out scenarios. Expected wall-clock:
   ~25 minutes single-threaded, ~5 minutes with `concurrent.futures`.
5. Re-render `bench/results/RESULTS.md`. Flip `deferred` to the
   measured fraction (e.g. `0.873`).
6. Update README hero table.
7. Cut `v0.8.1` patch release.

**Estimate**: 1 focused afternoon, ~$30 of API spend.

## Honesty audit

Before tagging, re-read the README and confirm each of these
statements is still defensible:

- [x] No `__.__%` placeholders remaining.
- [x] No "to be filled in by Week N" notes remaining.
- [x] Every number in the README hero table is either measured, marked
      `deferred` with a footnote, or marked `projected` with a
      footnote.
- [x] Limitations section explicitly mentions: `SELECT *` fan-out,
      Snowflake/BigQuery stubs only, polling cadence, LLM-drafted
      migrations require human review.
- [x] No claim of "production-tested at scale" appears anywhere.
- [x] No claim of "outperforms Great Expectations / dbt" appears
      without the qualifier that this is a synthetic corpus.

## Post-launch follow-ups (not blocking v0.8.0)

1. Real-OSS held-out slice (curated from dbt-core + Stripe schema
   repos + Airbnb Minerva).
2. First-class Snowflake adapter (~400 lines on top of the stub).
3. Debezium-driven sub-second mode.
4. Real OpenLineage `parentRunFacet` schema.
5. The funded Claude run that closes the `deferred` cell above.
6. 15-second demo GIF recorded against the live sandbox repo.
