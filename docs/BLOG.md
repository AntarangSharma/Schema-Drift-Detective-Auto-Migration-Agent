# Schema drift always wins. So I built an agent that gets paged first.

*Draft post for the v0.8.0 launch of [Schema Drift Detective](https://github.com/AntarangSharma/Schema-Drift-Detective-Auto-Migration-Agent). ~1500 words.*

---

## The 3am problem

Every data team I have ever worked with has the same outage pattern. An app team renames `user.email` to `user.email_address` on a Tuesday morning. dbt's nightly run on Wednesday goes green (the column still resolves through the staging view someone wrote two years ago). The Looker dashboard powering the CFO's Thursday board meeting renders empty. The CFO emails the CTO. The CTO emails me. I am on call.

The root cause is structural. Schema changes are *upstream* — they happen in an app database, a SaaS API, or a Kafka topic, owned by a team that does not run dbt. Quality tests are *downstream* — they live in a dbt project owned by a team that does not own the schema. The gap between them is where production-affecting drift hides.

The conventional answers to this gap are all unsatisfying in a specific way:

- **dbt source freshness + schema tests**: catches the *symptom* (a downstream test fails) hours or days after the cause, and only for changes that happen to break a test. Type-widening from `int` to `bigint` is silent. Adding a nullable column is silent. Tightening a `varchar(50)` to `varchar(20)` is silent until a row truncates.
- **Great Expectations**: same problem, dressed differently. Optimised for value-level expectations, not schema introspection.
- **"Just have the app team file a Slack ticket"**: works for engineering cultures that don't exist.

The pattern I wanted was the one that already works for code: a CI check that runs against the *diff*, not the deployed system. If a column gets dropped, a typed pull-request should open within seconds, against the dbt repo, with the blast radius computed, a draft migration written, and a rollback plan attached. A human reviewer accepts or rejects. The blast radius makes the rejection cheap.

That is what Schema Drift Detective does.

## What it actually does

A polling watcher (default: every 30 seconds) snapshots upstream schemas — currently Postgres, with DuckDB / Debezium / Snowflake adapters at varying degrees of "real". It diffs the new snapshot against the last persisted snapshot, classifies each diff into one of **13 typed `ChangeType`s** (`column_added_nullable`, `type_widened`, `precision_changed`, `column_dropped`, …), and emits a frozen `DriftEvent` Pydantic record.

That record gets walked through three layers:

1. **A lineage graph** built from dbt's `manifest.json` + a SQLGlot-driven column-level lineage pass. It handles CTEs, aliases, simple `JOIN`s, and `UNION ALL`, with a `fan_out_conservative=True` flag for `SELECT *` (every downstream column gets marked potentially-affected; we will not pretend we can resolve `*`).
2. **A policy engine** that decides between *ignore / alert / open a draft PR / open a PR*. It enforces a blast-radius cap (configurable), a kill-switch env var, rate limiting, and — critically — a destructive-change gate that means drops, narrows, renames, PK changes, and partition changes can *never* be auto-merged. The decision is recorded in an audit log.
3. **A migration drafter**. The rule-based path emits deterministic YAML patches against `dbt_project/models/sources.yml` using ruamel.yaml round-tripping (comments and key order preserved). For non-trivial SQL synthesis, an LLM drafter (Claude 3.5 Sonnet, swappable) produces a `MigrationProposal` with `Literal[col_names]` typing — the model literally cannot hallucinate a column name that does not exist on the post-state schema. A `dbt parse && dbt compile` validation loop retries up to twice with the compiler's error fed back.

The output is a single GitHub pull request, opened via PyGithub, against a configured target repo. It contains: the `DriftEvent`, a markdown-rendered impact set, a draft migration, an updated dbt sources file, and a rollback plan. **Every step writes to an audit record.** Every audit record is keyed on the same ULID as the PR branch name. You can answer "why did the agent open this PR" in one SQL query.

## What the benchmark says

I built a benchmark generator — `bench/generate.py` — that produces 364 synthetic scenarios across 18 base tables (TPC-H + NYC taxi + a Stripe-like OLTP schema) and 13 change types, with two variants each. A `sha256(scenario_id) % 10 ∈ {7, 8, 9}` split holds out 110 scenarios deterministically.

I scored four methods on the held-out slice:

| Method | Drift R | Class. R | Sev. F1 | Cost / 1k events |
|---|---|---|---|---|
| Great Expectations | 0.682 | 0.000 | 0.215 | $0 |
| dbt tests | 0.391 | 0.000 | 0.283 | $0 |
| One-shot Claude (pre + post snapshots pasted in) | 1.000 | 0.618 | 0.812 | ~$2.00 |
| **Ours (rule-based)** | **1.000** | **1.000** | **1.000** | **~$0.10** |

A few things deserve to be called out here, because I see too many launch posts that hide them.

**The rule path scores 1.000 because the benchmark was generated by the rule library.** This is the upper bound, not the headline. The honest reading is: "on rule-generated drift, we are deterministic and the LLM baseline is non-deterministic." Real-OSS drift will not score 1.000, and the post-v0.8.0 plan calls for a held-out slice harvested from dbt-core and Stripe schema-repo migration PRs.

**The one-shot LLM is a legitimate competitor on accuracy.** 0.618 classification recall and 0.812 severity F1 is *good*. The reason it does not win is operational: ~80× slower per round-trip, ~20× more expensive per 1k events, and re-running the held-out split shifts ~2% of classifications. None of that is acceptable for a CI gate that blocks merges.

**`Mig. ✓` is missing from the table, and I will not fake it.** The LLM drafter + the compile-retry loop is integration-tested end-to-end against a stubbed `DbtRunner` (`tests/test_llm.py`). What is missing is the corpus-level pass-rate over all 110 held-out scenarios, which requires (a) a funded Claude run and (b) a real `dbt-core` binary in CI. I refuse to publish a number extrapolated from `MockLLM` output. It is tracked openly in [`docs/06_launch_checklist.md`](06_launch_checklist.md) as the single deferred item.

## Where this is honest, and where it isn't

The launch is *honest* about its limits. They are written into the README, not hidden in a footnote:

- `SELECT *` in dbt models triggers conservative fan-out lineage. Every downstream column gets marked. This is a feature, not a bug — the alternative is silently under-reporting impact — but it does mean teams with `SELECT *`-heavy models will see large blast-radius numbers until they refactor.
- Tested on Postgres 16 + DuckDB 1.0 + dbt-core 1.8. Snowflake and BigQuery adapters are stubs that raise typed `NotImplementedError`s. They will compile-import cleanly so downstream code can target them, but they will not run.
- LLM-drafted migrations *always* require human review. The Pydantic validator enforces it: destructive change types simply cannot be auto-merged.
- 30-second polling cadence means drift-to-PR latency is seconds-to-minutes, not real-time. A Debezium-CDC mode is a stub.

It is *less honest* than I would like in two places, both flagged in `docs/06_launch_checklist.md`:

- The 30%-held-out split shares a generator with the training half. A real held-out slice would come from a different distribution. That is the post-launch real-OSS slice mentioned above.
- The latency numbers on the LLM rows are MockLLM. Real Claude is in the 800–1500 ms range. The table flags this with a `ᴹ` superscript, but you have to read the footnote.

## What I would build next

In rough priority order:

1. **The funded Claude run + the dbt-binary-in-CI image.** This is the single shippable thing that flips the `Mig. ✓` cell from `deferred` to a real number. Estimated cost: $15–30 of Claude tokens against 110 scenarios.
2. **Real-OSS held-out slice.** Curated from dbt-core's own migration history and a handful of public schema-repos. Re-measure RESULTS.md.
3. **First-class Snowflake adapter.** The stub typing is in place; the missing piece is a real `INFORMATION_SCHEMA` walker. Probably ~400 lines.
4. **A non-polling mode.** Debezium → drift in <1 second is achievable and undermines the "polling cadence" honest-limitation bullet.
5. **OpenLineage emission on every PR.** Half-shipped: `ol.py` emits when `OPENLINEAGE_URL` is set, no-ops otherwise. The half-shipped half is the schema for the OL event itself, which currently lies about its `parentRunFacet`.

## What I learned that I did not expect

Three things, briefly:

1. **Pydantic's `Literal[…]` is the most under-used LLM safety mechanism in the ecosystem.** Constraining the migration proposal's column names to a `Literal[…]` typed from the post-state snapshot eliminated an entire class of hallucination failures — the model literally cannot return a non-existent column. I did not have to write a single "did you hallucinate?" guardrail.
2. **The rule-based path is not the boring part.** I expected the LLM drafter to be the centrepiece. In practice, the deterministic classifier + the column-level lineage walk is what makes the PRs trustworthy enough to review at all. The LLM only writes the SQL bodies; the rule path tells you *which* SQL bodies need writing and *which* downstream models care.
3. **Sandbox PR repos are a launch-velocity multiplier.** I built a separate `drift-demo-sandbox` repo specifically so the agent could open real PRs against it without anyone reviewing them under time pressure. The live-PR integration test runs against it on every CI green, and the README links to the public PR feed. I will do this on every future agentic project.

---

*Schema Drift Detective is MIT-licensed. The 8-week build plan + ADRs are in [`docs/02_revised_plan.md`](02_revised_plan.md). The benchmark numbers + their caveats are in [`bench/results/RESULTS.md`](../bench/results/RESULTS.md). The launch checklist + the one deferred item are in [`docs/06_launch_checklist.md`](06_launch_checklist.md).*
