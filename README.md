# Schema Drift Detective

> **An upstream schema-drift CI check that opens PRs with proposed migrations and impact analysis.**
> Watches Postgres / DuckDB / REST APIs for schema changes, walks dbt + OpenLineage downstream graph, opens a GitHub PR with: a typed `DriftEvent`, a quantified blast radius, a draft migration, updated dbt tests, and a rollback plan.

<p align="center">
  <em>🚧 Phase 0 scaffold — see <a href="docs/02_revised_plan.md">docs/02_revised_plan.md</a> for the 8-week build plan.</em>
</p>

---

## Why

In most data teams, schema changes are *upstream* (app DB, SaaS API) and quality tests are *downstream* (dbt models, dashboards). The gap between them gets paged at 3am. This project closes the gap with a **deterministic detector + lineage-aware impact engine + LLM-drafted migration PR**.

> "Catches drift before it reaches production dashboards, with a quantified false-positive rate."

## Hero metrics _(to be filled in by Week 5)_

| | Value |
|---|---|
| Drift detection recall | `__.__%` |
| Severity classification macro-F1 | `__.__` |
| Downstream impact recall | `__.__` |
| Migration correctness (compiles + tests pass) | `__.__%` |
| Mean time: drift → PR | `__ s` |
| Steady-state cost | `~$2 / 1k events` |
| Scenarios in benchmark | `300` |

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

## Results _(to be filled in by Week 5)_

| Method | Det. R | Det. P | Sev. F1 | Impact R | Impact P | Mig. ✓ | FPR | MTTPR (s) | $/1k |
|---|---|---|---|---|---|---|---|---|---|
| B1: Great Expectations | _ | _ | _ | _ | _ | n/a | _ | n/a | $0 |
| B2: dbt tests | _ | _ | _ | _ | _ | n/a | _ | n/a | $0 |
| B3: One-shot LLM | _ | _ | _ | _ | _ | _ | _ | _ | $_ |
| **Ours (rule-only)** | _ | _ | _ | _ | _ | _ | _ | _ | $0.10 |
| **Ours (rule + LLM)** | _ | _ | _ | _ | _ | _ | _ | _ | $2.00 |

Reproduce: `make bench`.

## Honest limitations

- `SELECT *` in dbt models forces "fan-out conservative" lineage mode (all downstream columns marked potentially-affected).
- Tested on Postgres 16 + DuckDB 1.0 + dbt-core 1.8. Snowflake/BigQuery adapters are stubbed only.
- LLM-drafted migrations always require human review. Destructive change types (drop, narrow, rename, PK change, partition change) cannot be auto-merged — enforced at the Pydantic validator layer.
- Polling cadence (default 30s) means drift detection latency is in seconds-to-minutes, not real-time.

## Tech stack

Python 3.12 · Pydantic 2.7 · SQLGlot 25 · NetworkX 3 · dbt-core 1.8 · PyGithub 2.3 · Typer 0.12 · psycopg 3 · Postgres 16 · DuckDB 1.0 · OpenLineage 1.18 · Claude 3.5 Sonnet · GitHub Actions · ruff · pyright · pytest 8 · Hypothesis

## License

MIT. See [`LICENSE`](LICENSE).
