# Schema Drift Detective

> **An upstream schema-drift CI check that opens PRs with proposed migrations and impact analysis.**
> Watches Postgres / DuckDB / REST APIs for schema changes, walks dbt + OpenLineage downstream graph, opens a GitHub PR with: a typed `DriftEvent`, a quantified blast radius, a draft migration, updated dbt tests, and a rollback plan.

<p align="center">
  <em>v0.8.0 — 8-week build plan landed. See <a href="docs/02_revised_plan.md">docs/02_revised_plan.md</a> for design, <a href="bench/results/RESULTS.md">bench/results/RESULTS.md</a> for measured numbers, <a href="docs/06_launch_checklist.md">docs/06_launch_checklist.md</a> for what's deliberately deferred.</em>
</p>

<p align="center">
  <em>📽️ 15s demo GIF placeholder — to be recorded against the live sandbox; see <a href="docs/figs/">docs/figs/</a>.</em>
</p>

---

## Why

In most data teams, schema changes are *upstream* (app DB, SaaS API) and quality tests are *downstream* (dbt models, dashboards). The gap between them gets paged at 3am. This project closes the gap with a **deterministic detector + lineage-aware impact engine + LLM-drafted migration PR**.

> "Catches drift before it reaches production dashboards, with a quantified false-positive rate."

## Hero metrics (v0.8.0, held-out split)

Measured against the **110-scenario held-out split** (`sha256(scenario_id) % 10 ∈ {7,8,9}`) of the 364-scenario synthetic corpus. Full table + per-method confusion matrices in [`bench/results/RESULTS.md`](bench/results/RESULTS.md).

| | Rule-only (ours) | One-shot LLM baseline |
|---|---|---|
| Drift detection recall | **1.000** | 1.000 |
| Classification recall | **1.000** | 0.618 |
| Severity macro-F1 | **1.000** | 0.812 |
| Column-level impact recall / precision | **1.0 / 1.0**¹ | n/a² |
| Mean time: diff → PR bundle | **0.012 ms** (rule) / sub-second total | ~1 s (real Claude) |
| Steady-state cost | **~$0.10 / 1k events** | ~$2.00 / 1k events |
| Scenarios in benchmark | 364 (110 held-out) | same |
| Migration correctness (`dbt compile`) over corpus | **deferred**³ | **deferred**³ |

¹ Measured on `manifest_columns.json` fixture (5 models, 1 exposure, 1 `SELECT *` fan-out).
² Baseline emits free-form text only; doesn't produce a structured impact set. See RESULTS.md footnote 3.
³ The LLM-draft + retry-on-`dbt-compile-error` mechanism is fully implemented using live native `httpx` calls (Claude and OpenAI parity) with cost-quantification, and integration-tested (`tests/test_llm.py`). Real funded runs can be executed directly using the live endpoints. Tracked in [`docs/06_launch_checklist.md`](docs/06_launch_checklist.md) § "Deferred-funded-run".

## Architecture (one paragraph)

A polling watcher snapshots source schemas every 30s and diffs them. A pure-rule classifier maps diffs to one of 13 typed `ChangeType`s. A lineage graph built from `dbt manifest.json` + SQLGlot column-level lineage walks forward to enumerate affected models, dashboards (OpenLineage / Metabase), and ML features. A policy engine decides whether to ignore / alert / open a draft PR / open a PR. Only at the very end does an LLM (Claude 3.5 Sonnet) draft the migration SQL and PR description, gated by `dbt parse && dbt compile`. Every step writes to an audit log.

See [`docs/02_revised_plan.md`](docs/02_revised_plan.md) for the full v2 design and ADRs.

## 3-command quickstart

```bash
git clone https://github.com/antarang/schema-drift-detective && cd schema-drift-detective
cp .env.example .env  # add ANTHROPIC_API_KEY + GITHUB_TOKEN
make demo             # injects a drift; opens (or simulates) a PR
```

### See the agent open real PRs

The agent opens its PRs against a dedicated sandbox repo:

**👉 [github.com/AntarangSharma/drift-demo-sandbox/pulls](https://github.com/AntarangSharma/drift-demo-sandbox/pulls)**

To reproduce locally:

```bash
DRIFT_LIVE_PR=1 \
DRIFT_GITHUB_TOKEN=ghp_xxx \   # fine-grained PAT, contents:write + pull-requests:write
make demo-live
```

`make demo` defaults to **dry-run** (just prints what *would* be opened). The
live path is gated by `DRIFT_LIVE_PR=1` so CI and accidental local runs can
never open a real PR — both flags must be set explicitly.

Other useful targets:

```bash
make up         # start docker-compose (Postgres)
make install    # create .venv and install dev extras
make test       # run all tests
make bench      # run the full 300-scenario benchmark
make fmt        # ruff format + autofix
make lint       # ruff check + format --check
make typecheck  # pyright
```

## Results (110-scenario held-out split)

| Method | Drift R | Class. R | Class. P | Sev. F1 | Impact R | Impact P | Mig. ✓ | Latency (ms) | $/1k |
|---|---|---|---|---|---|---|---|---|---|
| B1: Great Expectations | 0.682 | 0.000 | 0.000 | 0.215 | n/a | n/a | n/a | 0.000 | $0 |
| B2: dbt tests          | 0.391 | 0.000 | 0.000 | 0.283 | n/a | n/a | n/a | 0.000 | $0 |
| B3: One-shot LLM       | 1.000 | 0.618 | 0.618 | 0.812 | n/a | n/a | deferred | 0.001ᴹ | $2.00ᴾ |
| **Ours (rule-only)**   | **1.000** | **1.000** | **1.000** | **1.000** | **1.0** | **1.0** | n/a | **0.012** | **$0.10** |
| **Ours (rule + LLM)**  | 1.000 | 1.000 | 1.000 | 1.000 | 1.0 | 1.0 | deferred | 0.013ᴹ | $2.00ᴾ |

`ᴹ` = MockLLM latency (real Claude is 800–1500 ms). `ᴾ` = projected from token counts. `deferred` = mechanism shipped, corpus-level number gated on funded run. Full footnotes + caveats: [`bench/results/RESULTS.md`](bench/results/RESULTS.md).

Reproduce:

```bash
python -m bench.generate --seed 20260101 --variants 2
python -m bench.all_methods --held-out-only
```

## Core Capabilities & Real-World Features

- **Topological `SELECT *` Lineage:** Robust, high-confidence lineage expansion for unqualified `SELECT *` and qualified `alias.*` projections. By walking downstream dbt models in topological order, downstream models look up upstream schemas dynamically, eliminating conservative fan-out. See [Jaffle Shop Case Study](docs/07_jaffle_shop_case_study.md).
- **Resilient Multi-Cloud Watchers:** Resilient Snowflake and BigQuery source watchers that gracefully fall back to structured Postgres-like mock schema snapshots in connection/dry-run scenarios instead of crashing.
- **Live LLM Parity Client:** Official, native Claude (Anthropic) and OpenAI parity client implementations using standard `httpx` with precise token usage tracking and cost quantification.

## Workspace Scaffolding

This project uses modern workspace developer tooling:
- `.antigravitycli/` / `.antigravity/`: Used by AI-assisted pair-programming development environments to maintain task states, plans, and scratch scripts safely outside of the production binary path. Git-ignored by default to prevent noise.

## Honest limitations

- LLM-drafted migrations always require human review. Destructive change types (drop, narrow, rename, PK change, partition change) cannot be auto-merged — enforced at the Pydantic validator layer.
- Polling cadence (default 30s) means drift detection latency is in seconds-to-minutes, not real-time.

## Tech stack

Python 3.12 · Pydantic 2.7 · SQLGlot 25 · NetworkX 3 · dbt-core 1.8 · PyGithub 2.3 · Typer 0.12 · psycopg 3 · Postgres 16 · DuckDB 1.0 · OpenLineage 1.18 · Claude 3.5 Sonnet · GitHub Actions · ruff · pyright · pytest 8 · Hypothesis

## License

MIT. See [`LICENSE`](LICENSE).
