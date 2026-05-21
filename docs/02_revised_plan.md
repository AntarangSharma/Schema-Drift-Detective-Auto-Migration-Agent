# Revised Build Strategy — v2

> Re-evaluation of `docs/01_initial_spec.md`. Date: 2026-05-21.
> This document supersedes the v0 plan. Read this one before building.

---

## TL;DR — What Changed and Why

After a hard look at v0, there are **10 strategic changes** that make the project *more accurate, more defensible in interviews, and faster to ship*. Most are subtractions, not additions.

| # | v0 (initial) | v2 (revised) | Why |
|---|---|---|---|
| 1 | Debezium + Redpanda CDC | **Information-schema polling** (every 30s) | CDC is operationally fragile in a demo, and real commercial drift tools (Datafold, Monte Carlo, Sifflet) poll. Polling works across Postgres/MySQL/Snowflake/BQ/DuckDB with zero infra. Recruiter-defensible: "I chose the boring, broadly-applicable design." |
| 2 | "Auto-merge low-severity PRs" | **Every PR requires human review**; agent never merges | Auto-merge is the single most attackable claim in an interview. Drop it. The narrative is "automated *proposal*, human *decision*" — same engineering, fewer scary words. |
| 3 | "Agent framework: LangGraph" | **Plain Python pipeline (~150 LOC)**, LangGraph as a 1-file *optional* adapter | LangGraph for 6 deterministic steps is overkill and a leaky abstraction in interviews. Show a clean class-based pipeline; mention LangGraph as "easy to swap in if you want streaming/checkpointing." |
| 4 | "Mine 60 scenarios from public dbt projects" | **100% programmatically-generated benchmark, 300 scenarios**, mined real-world as stretch only | Labels on mined data are noisy and partially copyrighted. Synthetic generation = provably correct labels + reproducible + scales to 1000+ scenarios with one command. |
| 5 | Metabase BI integration | **Marquez + OpenLineage** for downstream graph; Metabase as a stretch goal | OpenLineage is the open-standard data engineering interview answer. Marquez gives you a UI for free. Metabase is a nice-to-have, not core. |
| 6 | DuckDB *or* Postgres warehouse | **Postgres-as-warehouse AND DuckDB target**, both supported | Real teams use Postgres-as-warehouse for small/mid scale; supporting both shows range. Both are one-line dbt profile changes. |
| 7 | 6 weeks @ 10 hrs/wk = 60 hrs | **8 weeks @ 10 hrs/wk = 80 hrs**; weeks 7–8 are polish + launch | 60 hrs is unrealistic to ship a benchmark + agent + blog + demo. Either extend timeline or cut features. Extending is the honest choice. |
| 8 | Week 1 = "thin slice end-to-end" | **Day 3 = thin slice end-to-end** (`make demo` works locally) | Front-load the dopamine. By end of week 1 you should already be writing the benchmark. |
| 9 | Severity classification: LLM tiebreak for ambiguous cases | **Pure rules for v1, LLM only for migration SQL drafting** | LLM in the classification path is a liability (non-determinism, cost, attack surface). Rules are auditable. The benchmark will *prove* rules are enough for classification. |
| 10 | "Schema drift agent" framing | **"A CI check for upstream schema drift"** | This is the framing that interviewers like (`sqlfluff`, `dbt-checkpoint`, `pre-commit` are all respected). It's the same code with a humbler, more truthful label. |

**Net effect:** smaller surface area, stronger benchmark, less hand-wavy claims, more time for polish. Same resume bullets, more defensible in interviews.

---

## Strategic Decisions, Justified

### Decision 1: Polling over CDC (don't do Debezium for v1)

**Why polling is better here:**
- Works on every warehouse anyone might ask about (Postgres, Snowflake, BigQuery, Redshift, DuckDB, MySQL).
- Single dependency: `psycopg`. No Redpanda, no Kafka Connect, no Debezium config.
- Demo runs on a laptop in `docker compose up` with no JVM.
- The *narrative* in an interview is stronger: "I considered CDC; I picked polling because schema changes are slow-moving and CDC adds 3 moving parts for a 5-minute latency gain."
- Polling cadence (30s default, configurable) is sufficient — real teams catch drift in hours/days, not seconds.

**What we lose:** sub-second detection latency. *We don't need it for this problem.*

**What we keep as a stretch:** in Week 7, add a Debezium adapter to prove we can — it's one file implementing the `SourceWatcher` interface. This becomes a one-line resume bullet without eating core weeks.

### Decision 2: 100% synthetic benchmark (the project's actual moat)

This is the single biggest improvement over v0. Mining GitHub history was always going to be flaky:
- Commits that *look* like schema changes often aren't (renames in `schema.yml` without DB changes).
- Manual labeling of 60 scenarios = 6+ hours of subjective judgment that won't be reproducible by a reviewer.
- Real risk of LLM training-set contamination.

**The synthetic generator is the project.** A Python module `bench/generate.py` that:

```
For each of 13 change types:
  For each of N base tables (TPC-H + taxi + a stripe-like API schema):
    For each of K downstream depths {1, 2, 3, 4}:
      For each of J severity-relevant variants (with/without index, with/without
         downstream join, with/without alias, etc.):
         emit (pre_schema, post_schema, expected DriftEvent, expected ImpactSet,
               expected migration SQL).
```

13 × 8 × 4 × ~3 = **~1,200 candidate scenarios**; we curate down to 300 for v1 with stratified sampling so all change types and depths are represented.

**Properties:**
- 100% reproducible (`python -m bench.generate --seed 42`).
- 100% correct labels (we *constructed* the truth).
- Open-sourced as `schema-drift-bench` — a citable artifact, separately useful.
- Recruiter pitch: "I generated a public benchmark with 300 labeled scenarios; here's the leaderboard."

**Real-world appendix (Week 6, optional):** add a *small* (~30) hand-labeled scenario set from public OpenAPI changelogs (Stripe / Shopify / GitHub). This is the "does it generalize" sanity check, not the main eval.

### Decision 3: Reframe as "CI check for upstream schema drift," not "agent"

The interview attack surface on the word "agent" in 2026 is enormous. Every staff engineer will probe whether the LLM can hallucinate destructive changes. We sidestep entirely:

- **What the project does:** monitors schemas, classifies drift, opens PRs with proposed migrations and impact analysis. *Identical* to v0.
- **What we call it:** "an upstream schema drift CI check that opens PRs." Same artifact, less attack surface.
- **What the LLM does:** drafts migration SQL and the PR description. That's it. Everything else is rules + graph algorithms.

In an interview: "We considered making it more agentic with tool-use loops, but a deterministic pipeline with a single LLM-assisted code-generation step was more auditable. Here's the benchmark showing rules beat one-shot LLM on classification, and the LLM helps on the migration-drafting step specifically." Bulletproof.

### Decision 4: Marquez/OpenLineage over Metabase

- Marquez is open-source, has a free UI, and integrates with dbt via `dbt-ol`.
- OpenLineage events are a known data-engineering standard; mentioning it in interviews signals breadth.
- Metabase API integration is fragile (queries change schema; not all dashboards parse cleanly).
- **Stretch:** add Metabase in Week 7 as a 100-LOC adapter, mention in the README.

### Decision 5: Postgres-as-warehouse default, DuckDB as alternative target

Two `profiles.yml` configs, both work, both tested in CI. The default `make demo` uses a single Postgres for both the *source* and the *warehouse* (different schemas). This is radically simpler:

```
postgres
├─ schema: source_raw       ← the "source system"
└─ schema: analytics        ← the "warehouse" (dbt target)
```

One container, one connection string, the entire demo runs. DuckDB stays available as a second tested target for the "different warehouse" story.

---

## The Sharper Architecture (v2)

```
┌────────────────────────────────────────────────────────────────┐
│ Postgres 16                                                    │
│  ├─ schema source_raw  (the "source" — we ALTER it to drift)   │
│  └─ schema analytics   (dbt target)                            │
└──────────────┬─────────────────────────────────────────────────┘
               │ poll information_schema + pg_class every 30s
               ▼
   ┌────────────────────────────────────────────────────┐
   │ schema_drift.watcher.PostgresWatcher               │
   │   .snapshot() -> JSONSchema-like dict              │
   │   .diff(prev, curr) -> list[RawChange]             │
   └────────────────────┬───────────────────────────────┘
                        ▼
   ┌────────────────────────────────────────────────────┐
   │ schema_drift.classifier.Classifier (PURE RULES)    │
   │   .classify(RawChange) -> DriftEvent (no LLM)      │
   └────────────────────┬───────────────────────────────┘
                        ▼
   ┌────────────────────────────────────────────────────┐
   │ schema_drift.lineage.LineageGraph                  │
   │   built from dbt manifest.json + SQLGlot column    │
   │   lineage on compiled SQL                          │
   │   .impact(table, column) -> ImpactSet              │
   └────────────────────┬───────────────────────────────┘
                        ▼
   ┌────────────────────────────────────────────────────┐
   │ schema_drift.policy.PolicyEngine (PURE RULES)      │
   │   .decide(DriftEvent, ImpactSet) -> Action         │
   │     Action ∈ {OPEN_PR, OPEN_DRAFT_PR, ALERT_ONLY,  │
   │               IGNORE}                              │
   └────────────────────┬───────────────────────────────┘
                        ▼  (only if Action ∈ {OPEN_PR, DRAFT_PR})
   ┌────────────────────────────────────────────────────┐
   │ schema_drift.migrator.MigrationDrafter             │
   │   .draft(DriftEvent, ImpactSet) -> MigrationBundle │
   │     - dbt model patches (Jinja+libcst)             │
   │     - schema.yml test updates                      │
   │     - backfill.sql                                 │
   │     - rollback.sql                                 │
   │     - PR description.md  ← Claude 3.5 Sonnet here  │
   │     - migration.sql      ← Claude 3.5 Sonnet here  │
   │   Validated by: `dbt parse` + `dbt compile`        │
   └────────────────────┬───────────────────────────────┘
                        ▼
   ┌────────────────────────────────────────────────────┐
   │ schema_drift.pr.GitHubPRGateway (PyGithub)         │
   │   .open_pr(MigrationBundle) -> URL                 │
   └────────────────────────────────────────────────────┘

   Sidecars:
   • schema_drift.audit.AuditLog  (every step, every event)
   • schema_drift.metrics         (Prometheus /metrics)
   • OpenLineage emitter          (Marquez)
   • Slack notifier               (slack-sdk)
```

**Key architectural property:** the LLM is invoked **once**, late, only inside `MigrationDrafter`, and only to *write text* (migration SQL + PR description). Every decision before that is deterministic and auditable. This is the architecture sentence I want to be able to say in 15 seconds in interviews.

---

## File / Directory Layout (this is what the repo will look like)

```
schema-drift-detective/
├── README.md
├── LICENSE                       (MIT)
├── pyproject.toml
├── docker-compose.yml
├── Makefile
├── .github/workflows/
│   ├── ci.yml                    (pytest + bench on every PR)
│   └── deploy-demo.yml
├── docs/
│   ├── 01_initial_spec.md
│   ├── 02_revised_plan.md        ← this file
│   ├── 03_architecture.md        (the diagram + ADRs)
│   ├── 04_benchmark.md           (eval methodology)
│   ├── BLOG.md                   (1500-word blog draft)
│   └── figs/                     (architecture.png, confusion_matrix.png)
├── src/schema_drift/
│   ├── __init__.py
│   ├── models.py                 (Pydantic: DriftEvent, ImpactSet, etc.)
│   ├── watcher/
│   │   ├── base.py
│   │   ├── postgres.py
│   │   ├── rest.py
│   │   └── debezium.py           (stretch, week 7)
│   ├── classifier.py             (pure rules, 13 ChangeTypes)
│   ├── lineage.py                (dbt manifest + SQLGlot)
│   ├── policy.py                 (rules → Action)
│   ├── migrator.py               (Jinja + LLM drafter)
│   ├── pr.py                     (PyGithub)
│   ├── audit.py
│   ├── llm.py                    (single adapter; swap providers)
│   ├── ol.py                     (OpenLineage emitter)
│   ├── slack.py
│   └── cli.py                    (typer-based: drift watch, drift bench, …)
├── dbt_project/                  (the demo dbt project)
│   ├── dbt_project.yml
│   ├── profiles/profiles.yml
│   ├── models/staging/
│   ├── models/intermediate/
│   ├── models/marts/
│   └── seeds/
├── bench/
│   ├── generate.py               (THE benchmark generator)
│   ├── scenarios/                (300 generated YAMLs)
│   ├── runner.py
│   ├── baselines/
│   │   ├── ge_baseline.py
│   │   ├── dbt_tests_baseline.py
│   │   └── one_shot_llm_baseline.py
│   └── results/
│       ├── RESULTS.md
│       └── results.json
├── tests/                        (pytest; mirror src/ layout)
└── infra/
    ├── fly.toml
    └── main.tf                   (Terraform for Fly.io)
```

**Target line count for v1:** ~3,500 LOC src/ + ~1,500 LOC bench/ + ~1,000 LOC tests/. Anything bigger is a smell.

---

## Revised Week-by-Week Plan (8 weeks, ~10 hrs/week)

### Phase 0 — Setup (Days 1–2, ~4 hrs)
- [ ] `git init`, `pyproject.toml` (Python 3.12, ruff, pyright, pytest 8.x).
- [ ] `docker-compose.yml` with one Postgres 16 + one Marquez.
- [ ] Pre-commit hooks (ruff, ruff-format, pyright).
- [ ] `Makefile` targets: `make up`, `make down`, `make demo`, `make bench`, `make fmt`, `make test`.
- [ ] Empty `src/schema_drift/` with `models.py` containing the Pydantic schema from v0 §1.7.
- [ ] CI workflow that runs `pytest` (will be empty initially but green).
**DoD:** `make up && make test` passes on a clean clone.

### Week 1 — MVP thin slice (Days 3–7, ~10 hrs)
**Outcome:** by end of Day 3, `make demo` injects a nullable column add and opens a real PR. Days 4–7 expand to 4 change types.

- [ ] **Day 3 — the thin slice:**
  - `PostgresWatcher.snapshot()` returns columns from `information_schema.columns`.
  - `PostgresWatcher.diff()` computes set-difference between two snapshots.
  - `Classifier` handles `COLUMN_ADDED_NULLABLE` only.
  - `LineageGraph.from_manifest()` reads `target/manifest.json`, builds a node-level NetworkX DiGraph.
  - `LineageGraph.impact()` does forward BFS (column-level lineage comes later).
  - `MigrationDrafter` for nullable add: append column to `schema.yml`, no SQL change needed.
  - `GitHubPRGateway.open_pr()` against your own fork.
  - Wire it all together in `cli.py`'s `drift watch --once` command.
- [ ] **Day 4:** add `COLUMN_DROPPED` (high-severity, draft PR with BREAKING label).
- [ ] **Day 5:** add `TYPE_WIDENED` and `TYPE_NARROWED`.
- [ ] **Day 6:** rule engine cleanup; introduce `Action` enum and `PolicyEngine`.
- [ ] **Day 7:** record a 30-second screencast of `make demo`. (Doing this *early* — not at the end — is a forcing function for "does the demo actually work?")

**DoD:** 4 change types end-to-end. Loom recorded. PR template renders with impact set.
**Risk:** GitHub API quirks. **Mitigation:** dry-run mode is the first feature, not the last.
**Cut here if behind:** skip Day 7 screencast.

### Week 2 — The benchmark (this is the moat)
**Outcome:** `python -m bench.generate` produces 300 scenarios; `python -m bench.runner --method ours` produces a results.json.

- [ ] Implement all 13 ChangeType classifiers (pure Python; one test per type).
- [ ] Write `bench/generate.py`:
  - Base catalog: TPC-H 8 tables + 4 NYC-taxi tables + 6 Stripe-like API tables = 18 base tables.
  - Generator function per ChangeType produces pre_schema/post_schema/expected DriftEvent.
  - Generator constructs a small dbt project per scenario *as well* (so impact is computable).
- [ ] Stratified sample to 300 (balance change types × depth × severity).
- [ ] `bench/runner.py` with `--method {ours,ge,dbt,oneshot}` flag.
- [ ] First fill of `RESULTS.md`: "Ours (rule-only)" column populated.
- [ ] Release `schema-drift-bench` as a separate sub-package on PyPI Test [optional].

**DoD:** Reviewer can clone repo and run `make bench` in < 15 minutes; output a Markdown table.
**Risk:** generator complexity explodes. **Mitigation:** start with 1 change type × 5 base tables = 5 scenarios; iterate.
**Cut here if behind:** 150 scenarios is fine for v1.

### Week 3 — Column-level lineage (the hardest week)
**Outcome:** lineage that walks columns, not just models.

- [ ] Use `sqlglot.lineage.lineage()` on dbt-compiled SQL for each model.
- [ ] Build a column-level NetworkX DiGraph (`(model, column)` nodes).
- [ ] Handle: CTEs, aliases, simple `JOIN`, `UNION ALL`. Document `SELECT *` as "fan-out conservative" mode.
- [ ] `LineageGraph.impact_columns(source_table, column)` returns affected `(model, column)` pairs.
- [ ] Property tests with Hypothesis: round-trip lineage on generated SQL.
- [ ] Update `RESULTS.md` Impact R/P columns.

**DoD:** Impact recall ≥ 0.85 on the benchmark held-out split.
**Risk:** SQLGlot edge cases on real dbt SQL. **Mitigation:** maintain a `lineage_known_failures.yml` and skip those scenarios with a documented note (better than silently wrong).
**Cut here if behind:** column-level lineage for SELECT-projection only; star-projection falls back to "all downstream columns potentially affected."

### Week 4 — Migration drafting + LLM integration
**Outcome:** LLM produces migration SQL that passes `dbt compile` for ≥ 70% of HIGH-severity scenarios.

- [ ] `src/schema_drift/llm.py`: single thin adapter, swappable Claude/OpenAI.
- [ ] Structured output via `instructor` + Pydantic `MigrationProposal` model.
- [ ] Prompt template lives in `prompts/migration_drafter.md` (versioned, not f-strings).
- [ ] Runtime constraint: column names in the proposal *must* be a `Literal[...]` derived from the manifest. Reject and retry if not.
- [ ] Validation: subprocess `dbt parse && dbt compile`; if it fails, retry up to 2× with the error as feedback.
- [ ] Cost tracking: log token counts to `audit_log` per call.

**DoD:** Migration correctness ≥ 0.70 on HIGH-severity slice; cost per event ≤ $0.05 average.
**Risk:** LLM is non-deterministic; eval results jitter. **Mitigation:** seed temperature=0; run benchmark 3 times and report mean + std.
**Cut here if behind:** drop backfill.sql; emit migration.sql + tests only.

### Week 5 — Baselines + final benchmark
**Outcome:** results table filled in completely; honest write-up.

- [ ] B1: Great Expectations 0.18 harness — replay drift, see failures.
- [ ] B2: dbt tests harness — `not_null`/`unique`/`accepted_values` per source.
- [ ] B3: one-shot LLM — single Sonnet call with pre+post schema + manifest pasted in.
- [ ] Run all 5 methods on the frozen held-out split (`sha256 mod 10 ∈ {7,8,9}`).
- [ ] Confusion matrix plot (matplotlib → `docs/figs/`).
- [ ] Write `bench/results/RESULTS.md` with table + 3 paragraphs of analysis.
- [ ] Contamination canary section: report on the 10 invented table-name scenarios.

**DoD:** Table is fully populated; results reproducible on a clean clone via `make bench`.
**Risk:** Ours loses to B3 on some metric. **Mitigation:** report honestly. Note where & why — interviewer respects that more than a clean sweep.
**Cut here if behind:** B1 (Great Expectations) is optional.

### Week 6 — Guardrails, OpenLineage, polish
**Outcome:** all guardrails functional; OpenLineage emission; Slack notification; docs.

- [ ] Implement all guardrails from v0 §1.6 (blast-radius cap, dry-run, kill switch, rate limit, audit log).
- [ ] OpenLineage events emitted from each pipeline step; verify in Marquez UI.
- [ ] Slack notification with Block Kit; one Slack message per PR opened.
- [ ] `docs/03_architecture.md` written with the v2 diagram and 4 ADRs (polling-vs-CDC, rules-vs-LLM, manifest-vs-info_schema, synthetic-vs-mined).
- [ ] Prometheus `/metrics` endpoint: drift events / sec, classification latency p50/p99, LLM cost / hour.
- [ ] Hand-label ~30 real-world scenarios from Stripe/Shopify/GitHub OpenAPI changelogs (optional appendix).

**DoD:** all guardrails covered by tests; Marquez screenshot in README.
**Cut here if behind:** skip the real-world appendix; Slack/Prometheus can be stubs with TODOs.

### Week 7 — Stretch goals + Debezium adapter
**Outcome:** the "more bullet points for the resume" week.

- [ ] Debezium adapter (`src/schema_drift/watcher/debezium.py`) — one file, ~200 LOC, optional.
- [ ] Metabase adapter (~150 LOC) emitting dashboards into the impact set.
- [ ] DuckDB end-to-end smoke test (different `profiles.yml`).
- [ ] Snowflake adapter stub with a note in README (do *not* claim it works without testing).
- [ ] Performance pass: target `make bench` < 10 minutes on a MacBook.

**DoD:** README "Supports" matrix is honest and tested.

### Week 8 — Launch
**Outcome:** repo + blog + Loom + LinkedIn + tweet thread are live.

- [ ] Final README pass; ensure 30-second scan tells the whole story.
- [ ] Deploy to Fly.io with a public read-only Marquez UI.
- [ ] Record final 90-second Loom (replaces Day 7 draft).
- [ ] Blog post (~1500 words) — finish from `docs/BLOG.md` outline.
- [ ] Submit to *Locally Optimistic*, *Data Engineering Weekly*, *r/dataengineering*.
- [ ] Schedule LinkedIn + Twitter thread.
- [ ] Update LinkedIn headline + add to resume.

**DoD:** Recruiter scans README → understands the project in 30 seconds → can run `make demo` in 3 commands.

---

## What I Will Not Build (Saying No Explicitly)

These came up in v0 thinking and are *deliberately* out of scope:
- **No Looker / Tableau integration.** They need licenses; Marquez is enough.
- **No multi-warehouse adapters beyond Postgres/DuckDB tested.** Snowflake/BigQuery get a stubbed `Profile` class with a "not tested" warning.
- **No web UI.** The "interface" is the PR. A UI is a different project.
- **No fine-tuning of an LLM.** Sonnet + good prompts + Pydantic-enforced output is enough for the eval; fine-tuning is a "future work" bullet.
- **No real-time streaming.** Polling every 30s is enough.
- **No Kafka in v1.** Stretch only.
- **No multi-tenant.** Single-org assumption everywhere.
- **No "self-healing" loops.** Agent proposes; human merges. Period.

Writing these down means interviewers can't bait you with "did you consider X?" — you have a 1-sentence answer for each.

---

## Defensive Interview Cheat-Sheet (built into the design)

| Likely interview question | Pre-built answer (because v2 was designed around it) |
|---|---|
| "Why not Debezium?" | "I considered it; polling is cross-warehouse, requires no JVM, and drift latency in minutes is fine. Debezium adapter exists in `watcher/debezium.py` as a 200-LOC swap." |
| "How do you trust an LLM to write migrations?" | "I don't fully. Classification is rule-based and audited. The LLM drafts SQL inside a validation loop (`dbt parse && dbt compile`) with column names enforced as `Literal[...]` from the manifest. Destructive ops never auto-merge." |
| "What about hallucinated columns?" | "Pydantic + Instructor force column names into a `Literal` set derived at runtime from the live manifest. A hallucinated column raises a validation error and the call retries." |
| "How do you handle `SELECT *`?" | "Documented limitation. Falls back to conservative fan-out: every downstream column is marked potentially affected. See lineage.py:safe_parse." |
| "What's the false-positive rate?" | "0.0X on the benchmark's benign-change slice (50 cosmetic changes). See `RESULTS.md`." |
| "How would this scale to 1000 tables?" | "Lineage graph is built once, cached, invalidated on dbt run. Schema snapshots are diff-only. The expensive call (LLM) only fires on actual drift events, ~tens/day at most for a real org." |
| "Why not just data contracts?" | "Contracts assume upstream cares. In practice, app teams ship; data teams cope. This is the fallback for that reality." |
| "Show me a test." | Point at `tests/test_classifier.py` — 13 ChangeTypes × 3 cases each = 39 deterministic unit tests. |

---

## What Success Looks Like at Week 8

A reviewer (recruiter / hiring manager / senior eng) lands on the repo and within 60 seconds sees:

1. A hero metric line: *"Detects 96% of schema drifts before they reach production. Beats Great Expectations, dbt tests, and one-shot LLM on 300 labeled scenarios."*
2. A 15-second GIF: ALTER TABLE → 30s later, a GitHub PR opens with a labeled impact report.
3. A reproducible benchmark with a public results table.
4. A clean repo: ~3,500 LOC, well-tested, no dead code, ruff-clean, type-checked.
5. A blog post explaining *why* (the lineage insight) and *what didn't work*.

That's the bar. Everything in this v2 plan is in service of that.

---

## Decision Point: Are We Going With v2?

If yes — next steps:

1. Confirm v2 is the plan. (Reply: "go" or "go with changes: ...")
2. I will create the Phase-0 scaffold: `pyproject.toml`, `docker-compose.yml`, `Makefile`, `src/schema_drift/models.py`, empty CI workflow, and the first commit-worthy README skeleton.
3. We then execute Day 3 of Week 1 as a single focused session to ship the thin slice.

If you want changes, the most likely candidates to push back on:
- **Timeline:** want to compress back to 6 weeks? We'd cut Week 7 stretch and Week 6 polish; result is a thinner but still shippable project.
- **Warehouse choice:** want Snowflake-first instead of Postgres? Possible but slows the demo (cloud credentials in `make demo` is a non-starter).
- **LLM provider:** want OpenAI-only or local model? Trivial swap in `llm.py`.
- **Framing:** want to keep the "agent" word in the title for SEO/buzz? OK — keep the title, change the body. The implementation is the same.
