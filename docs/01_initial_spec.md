# Schema Drift Detective + Auto-Migration Agent — Initial Spec

> Generated 2026-05-21. This is the v0 spec. See `docs/02_revised_plan.md` for the re-evaluated build strategy.

## Pre-flight: Scope Critique (3 bullets)

- **"Auto-migration agent" is the riskiest framing.** Recruiters and staff engineers will instinctively distrust an LLM that writes migrations against production warehouses. Reposition as **"Schema Drift Detective with PR-assisted migrations"** — the agent *proposes* migrations, humans merge. Same code, less skepticism, fewer "what about destructive ops?" interview traps.
- **Cut SaaS connectors and Kafka from v1.** Postgres CDC (Debezium) + REST API polling covers 90% of the narrative. Kafka schema registry is a great *bullet point* you can add in week 6 as a 200-line stretch goal; building it real costs you 2 weeks.
- **The benchmark is the moat, not the agent.** A portfolio project with 200 labeled drift scenarios + a public leaderboard beating Great Expectations is more defensible than any agent architecture. Spend Week 2 disproportionately on the eval harness; it doubles as a blog post on its own.

---

# DELIVERABLE 1 — ARCHITECTURE

## 1.1 Component Diagram (text)

```
┌─────────────────────────────────────────────────────────────────────┐
│                         SOURCE SYSTEMS                              │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐    │
│  │ Postgres 16  │  │ REST APIs    │  │ Kafka 3.7 (stretch)    │    │
│  │ (orders db)  │  │ (Stripe-like)│  │ + Confluent SR 7.6     │    │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬────────────┘    │
└─────────┼─────────────────┼──────────────────────┼─────────────────┘
          │ logical repl    │ poll /v1/schema      │ Avro schema evt
          ▼                 ▼                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    CHANGE CAPTURE LAYER                             │
│  ┌──────────────────────┐    ┌──────────────────────────────────┐  │
│  │ Debezium 2.7         │    │ APScheduler poller               │  │
│  │ → schema_changes     │    │ → hashes information_schema      │  │
│  │   topic (Redpanda)   │    │   + REST /schema endpoints       │  │
│  └──────────┬───────────┘    └────────────┬─────────────────────┘  │
└─────────────┼─────────────────────────────┼─────────────────────────┘
              ▼                             ▼
        ┌──────────────────────────────────────────┐
        │  Drift Normalizer (Python 3.12, Pydantic)│
        │  • Diffs JSON-Schema reps                │
        │  • Emits DriftEvent objects              │
        └────────────────┬─────────────────────────┘
                         ▼
        ┌──────────────────────────────────────────┐
        │  Schema Registry (Postgres table)        │
        │  • schemas(snapshot_id, source, ts, blob)│
        │  • drift_events(id, type, severity, …)   │
        └────────────────┬─────────────────────────┘
                         ▼
   ┌─────────────────────────────────────────────────────────┐
   │             IMPACT ANALYZER (LangGraph)                 │
   │  Node 1: classify_drift     (deterministic + LLM tiebreak)│
   │  Node 2: walk_lineage       (dbt manifest + SQLGlot)    │
   │  Node 3: enumerate_bi       (Metabase API)              │
   │  Node 4: score_blast_radius (graph algo, no LLM)        │
   │  Node 5: draft_migration    (LLM w/ structured output)  │
   │  Node 6: validate_migration (dbt compile + dry-run)     │
   └────────────────────────┬────────────────────────────────┘
                            ▼
        ┌──────────────────────────────────────────┐
        │  PR Generator (PyGithub 2.3)             │
        │  • Branch, commit, open PR, attach report│
        └───┬──────────────────────────────────────┘
            ▼
   ┌─────────────────────┐    ┌─────────────────────┐
   │ GitHub repo         │    │ Slack #data-drift   │
   │ (dbt project)       │    │ via Block Kit       │
   └─────────────────────┘    └─────────────────────┘

   Sidecars:
   • Prometheus /metrics on agent  • Grafana dashboard
   • Loki for logs                 • OpenLineage events to Marquez
```

## 1.2 Stack with one-sentence justification

| Concern | Choice | Why |
|---|---|---|
| Source CDC | **Debezium 2.7 → Redpanda 24.1** | Industry-standard, captures DDL events on Postgres 16 logical replication; Redpanda over Kafka because single binary = portfolio-friendly local dev. |
| REST polling | **APScheduler 3.10 + httpx 0.27** | Sufficient for SaaS-style sources; avoids over-engineering with Airflow for cron. |
| Schema registry | **Postgres table `schema_snapshots`** | Confluent SR only buys you Avro/Protobuf; for cross-source (REST + SQL) a homegrown JSON-Schema store is simpler and recruiter-explainable. |
| Lineage | **dbt-core 1.8 `manifest.json` + SQLGlot 25.x + OpenLineage 1.18** | dbt manifest is ground truth for the warehouse; SQLGlot for parsing ad-hoc SQL; OpenLineage for emitting events to Marquez for the demo. |
| BI lineage | **Metabase 0.50 API** | Free, has `/api/card` returning the SQL of every dashboard card; Looker/Tableau need licenses. |
| Agent framework | **LangGraph 0.2** | Explicit state machine beats LangChain agents for a 6-node deterministic flow; easy to draw on a whiteboard in an interview. |
| LLM | **Claude 3.5 Sonnet (primary), GPT-4o-mini (cheap classifier)** [ASSUMPTION: pricing as of 2026-05] | Sonnet for migration drafting; mini for the per-event severity tiebreak to keep cost down. |
| Structured output | **Instructor 1.3 + Pydantic 2.7** | Forces JSON conformance; eliminates a class of parse failures. |
| PR automation | **PyGithub 2.3** | Boring, official, handles branch + commit + PR + labels in 40 LOC. |
| Notifications | **slack-sdk 3.27 (Block Kit)** | One channel, signed webhooks, no Slack app pain. |
| Storage | **Postgres 16 (events, snapshots)** + **DuckDB 1.0 (warehouse for dbt)** | DuckDB lets the demo run on a laptop while still being a "real" warehouse dbt targets. |
| Deployment | **Docker Compose locally; Fly.io for hosted demo** | Compose for reviewers who clone the repo; Fly.io because Railway/Render have wrecked too many demos. |
| Observability | **Prometheus 2.53 + Grafana 11 + Loki 3.0** | Shows you can instrument; one screenshot in README earns credibility. |
| CI | **GitHub Actions** | dbt build + pytest + the drift benchmark on every PR. |
| IaC | **Terraform 1.8** (Fly.io provider) | One file, but signals you know IaC matters. |

## 1.3 Drift Taxonomy (13 types)

| # | Change Type | Severity | Auto-Remediation Policy |
|---|---|---|---|
| 1 | Column added, nullable | **LOW** | Auto-add to dbt source `.yml`, add `not_null=false` test, open PR — auto-mergeable. |
| 2 | Column added, NOT NULL with default | **LOW** | Same as #1; capture default in docs. |
| 3 | Column added, NOT NULL no default | **MEDIUM** | Open PR but require human approval; backfill plan generated. |
| 4 | Column dropped | **HIGH (destructive)** | NEVER auto-merge; PR opens with `BREAKING` label + downstream impact list. |
| 5 | Type widened (int→bigint, varchar(50)→varchar(255)) | **LOW** | Auto-PR; update dbt model casts if any. |
| 6 | Type narrowed (bigint→int, numeric(10,2)→numeric(8,2)) | **HIGH** | Block; generate backfill validation query; require approval. |
| 7 | Type changed incompatibly (int→text) | **HIGH** | Block; LLM proposes cast strategy as comment, not commit. |
| 8 | Column renamed | **HIGH** | Detected via heuristic (drop+add same type, similar name via Levenshtein ≤ 3); PR renames refs across dbt models. |
| 9 | Precision/scale change on numeric | **MEDIUM** | Auto-PR with `dbt test` for value range. |
| 10 | PK / unique constraint change | **HIGH** | Block; flag every downstream model using that key. |
| 11 | Enum value added | **LOW** | Auto-PR updating `accepted_values` test. |
| 12 | Default value changed | **MEDIUM** | Auto-PR with audit comment; flag if column is used in `COALESCE`. |
| 13 | Partition key / clustering change | **HIGH** | Block; performance regression risk. |
| 14 | NOT NULL added to existing nullable | **MEDIUM** | Auto-PR + validation query for nulls before merge. |

## 1.4 Lineage-Aware Impact Algorithm

```
Input: DriftEvent(source_table, column, change_type)
Output: ImpactSet(models[], dashboards[], features[], severity_score)

1. Build dependency graph G:
   • Nodes: dbt models from manifest.json (~few hundred for a demo project)
   • Edges: from manifest's `depends_on.nodes`
   • Augment with column-level lineage via SQLGlot:
       parsed = sqlglot.parse_one(model.compiled_sql, dialect='duckdb')
       for col_ref in parsed.find_all(exp.Column):
           record (model, col_ref.name) -> (upstream_model, upstream_col)

2. Seed = {source_table.column}
3. BFS forward through G, propagating only nodes whose column-lineage
   touches the seed. (This is the cheap, deterministic, NO-LLM core.)

4. For each affected dbt model, query Metabase /api/card:
       cards where dataset_query.native.query CONTAINS model_name
   → list of dashboards.

5. Score blast radius:
       score = w1 * |models_affected|
             + w2 * |dashboards_affected|
             + w3 * has_ml_feature_flag
             + w4 * severity_weight(change_type)
   [ASSUMPTION] w1=1, w2=3, w3=5, w4=∈{1,3,10} → tune on eval set.

6. LLM is invoked ONLY for:
   (a) Rename detection when heuristic confidence is in [0.4, 0.8]
   (b) Migration SQL drafting (step in §1.5)
   (c) Human-facing PR description prose
   Steps 1–5 are pure Python / SQLGlot. Be loud about this in interviews.
```

## 1.5 Migration Generator

For each `DriftEvent` with policy = auto-PR:

1. **dbt model patch**: Use `libcst`-style AST edits on the `.sql` file when column refs need rewriting; for simple cases (new column add), append to `select` clause with a generated alias.
2. **`schema.yml` test updates**: Templated via Jinja2 — `not_null`, `accepted_values`, `dbt_utils.expression_is_true` ranges.
3. **Backfill DAG**: Emit a `models/migrations/{date}_{change_id}.sql` file containing:
   - `CREATE TABLE … AS SELECT …_v2` (idempotent: `IF NOT EXISTS`)
   - A `dbt run-operation validate_backfill --args '{change_id: ...}'` step
   - A swap step gated by a `dbt-checkpoint` test
4. **Rollback**: Every PR includes a `rollback.sql` file produced by inverting the diff. For destructive changes, rollback requires the pre-change snapshot stored in `schema_snapshots`.
5. **Idempotency**: Every generated file is keyed by `sha256(drift_event.id + change_type + target)`. Re-running the agent never produces duplicate PRs.

## 1.6 Guardrails

- `--dry-run` flag: emits the PR body to stdout, no GitHub call.
- **Blast-radius cap**: if `|models_affected| > 25` OR any dashboard is tagged `tier:critical`, the agent refuses to open a PR and instead posts a Slack alert.
- **Human approval gate**: any severity ∈ {HIGH} or change_type ∈ {drop, narrow, rename, PK change} opens PR as `draft=True` with `requires-human` label.
- **Audit log**: every action writes to `audit_log` table with the LLM prompt/response hash; queryable via `/audit` endpoint.
- **Rate limit**: max 10 PRs/hour per repo.
- **LLM kill switch**: env var `LLM_DISABLED=1` falls back to rule-only mode.

## 1.7 Pydantic Schema: `DriftEvent`

```python
from datetime import datetime
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

class ChangeType(str, Enum):
    COLUMN_ADDED_NULLABLE = "column_added_nullable"
    COLUMN_ADDED_NOT_NULL = "column_added_not_null"
    COLUMN_DROPPED = "column_dropped"
    TYPE_WIDENED = "type_widened"
    TYPE_NARROWED = "type_narrowed"
    TYPE_INCOMPATIBLE = "type_incompatible"
    COLUMN_RENAMED = "column_renamed"
    PRECISION_CHANGED = "precision_changed"
    PK_CHANGED = "pk_changed"
    ENUM_VALUE_ADDED = "enum_value_added"
    DEFAULT_CHANGED = "default_changed"
    PARTITION_KEY_CHANGED = "partition_key_changed"
    NULLABILITY_TIGHTENED = "nullability_tightened"

class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class ColumnSpec(BaseModel):
    name: str
    data_type: str
    nullable: bool
    default: Optional[str] = None
    is_primary_key: bool = False

class ImpactSet(BaseModel):
    dbt_models: list[str] = Field(default_factory=list)
    dashboards: list[dict] = Field(default_factory=list)
    ml_features: list[str] = Field(default_factory=list)
    blast_radius_score: float = 0.0

class DriftEvent(BaseModel):
    id: str
    detected_at: datetime
    source_system: Literal["postgres", "rest_api", "kafka"]
    source_identifier: str
    change_type: ChangeType
    severity: Severity
    column_before: Optional[ColumnSpec] = None
    column_after: Optional[ColumnSpec] = None
    confidence: float = Field(ge=0.0, le=1.0)
    impact: ImpactSet
    proposed_migration_pr: Optional[str] = None
    auto_mergeable: bool = False
    requires_backfill: bool = False
    rollback_plan_path: Optional[str] = None
    audit_trail: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_destructive_gate(self):
        destructive = {
            ChangeType.COLUMN_DROPPED,
            ChangeType.TYPE_NARROWED,
            ChangeType.TYPE_INCOMPATIBLE,
            ChangeType.PK_CHANGED,
        }
        if self.change_type in destructive and self.auto_mergeable:
            raise ValueError("Destructive changes cannot be auto-mergeable.")
        return self
```

## 1.8 Cost Model

[ASSUMPTIONS] 70% rule-only (0 tokens), 25% mini tiebreak (~800 in / 200 out), 5% Sonnet drafting (~4k in / 1.5k out).

Per 1000 events:
- Mini: 250 × (800 × $0.15 + 200 × $0.60) / 1M = **$0.06**
- Sonnet: 50 × (4000 × $3 + 1500 × $15) / 1M = **$1.73**
- Infra (Fly.io shared-cpu-2x amortized): **~$0.20**
- **Total: ~$2.00 / 1000 drift events.**

Tagline: "One on-call page costs more than 10,000 drift events through this agent."

---

# DELIVERABLE 2 — EVAL DATASET & METHODOLOGY

## 2.1 Building 210 Labeled Scenarios

- **120 synthetic injections** into TPC-H SF1 + NYC Yellow Taxi 2023 Q1 loaded into DuckDB; a real dbt project with 14 staging / 8 intermediate / 6 marts / 4 exposures / 2 ML feature views.
- **60 mined from public dbt projects**: `gitlab-data/analytics`, `dbt-labs/jaffle_shop`, `dbt-labs/mrr-playbook`, `Tomme/dbt-airbyte`. Use `pydriller 2.7` to walk history; manually label 60 (~6 hours).
- **30 from public API changelogs**: Stripe, Shopify, GitHub REST. Use `openapi-diff` on two release tags.

Scenario YAML format:
```yaml
id: tpch_orders_001
source_kind: postgres
pre_schema: tpch_orders_v1.json
post_schema: tpch_orders_v2.json
ground_truth:
  change_type: column_added_nullable
  severity: low
  affected_models: [stg_orders, fct_orders, mart_revenue_daily]
  affected_dashboards: [exec_revenue]
  migration_sql_path: expected_migrations/tpch_orders_001.sql
provenance: synthetic
```

## 2.2 Metrics

- **Detection recall** `R_det = TP / (TP+FN)`
- **Detection precision** `P_det = TP / (TP+FP)`
- **Severity F1**: macro-F1 across {LOW, MED, HIGH} from 13×13 confusion matrix.
- **Downstream-impact recall/precision**: per-scenario intersection over predicted/true affected assets.
- **Migration correctness**: 3 gates — `dbt compile` ✓, `dbt test` ✓, backfill checksum match.
- **False-positive rate on benign changes**: inject 50 benign changes; `FPR = FP_benign / 50`.
- **MTTD**: wall-clock from pg_logical event → DriftEvent row.
- **MTTPR**: DriftEvent row → GitHub PR created.

## 2.3 Baselines

| Baseline | Setup |
|---|---|
| **B1: Great Expectations 0.18** | Run GE checkpoint suite, replay drift, observe failures. |
| **B2: dbt source freshness + tests** | `source freshness` + `not_null` + `unique` + `accepted_values`. |
| **B3: Single-LLM-shot** | One Sonnet call with pre/post schema + full manifest pasted in. |

## 2.4 Contamination Check

- Temporal holdout: GitHub-mined commits `>= 2025-10-01` only.
- Deterministic name salting on older commits (`customer_id → cust_uid_47`).
- Hash-based split: `sha256(scenario_id) % 10`; 0–6 dev, 7–9 frozen test.
- Canary: 10 invented table names ("zorblax_transactions") to measure leakage.

## 2.5 Results Table Template

| Method | Det. R | Det. P | Sev. F1 | Impact R | Impact P | Mig. correct | FPR | MTTPR (s) | $/1k |
|---|---|---|---|---|---|---|---|---|---|
| B1: GE | __ | __ | __ | __ | __ | n/a | __ | n/a | $0 |
| B2: dbt tests | __ | __ | __ | __ | __ | n/a | __ | n/a | $0 |
| B3: One-shot LLM | __ | __ | __ | __ | __ | __ | __ | __ | $__ |
| **Ours (rule-only)** | __ | __ | __ | __ | __ | __ | __ | __ | $0.10 |
| **Ours (rule + LLM)** | __ | __ | __ | __ | __ | __ | __ | __ | $2.00 |

Target: Detection R ≥ 0.95, Sev F1 ≥ 0.85, Impact R ≥ 0.90, Migration ≥ 0.75, FPR ≤ 0.05.

---

# DELIVERABLE 3 — WEEK-BY-WEEK BUILD PLAN

### Week 1 — Thin slice end-to-end
Postgres column-add → DriftEvent → walk manifest → PR. `make demo` < 60s.
Cut: drop Debezium for polling.

### Week 2 — Drift taxonomy + benchmark v0
All 13 change types classifiable; 80 scenarios labeled; `pytest bench/` works.
Cut: ship 50 scenarios.

### Week 3 — Impact analyzer + BI integration
Column-level lineage; Metabase dashboards in PR body; +60 mined scenarios.
Cut: drop Metabase to week 6.

### Week 4 — LangGraph + migration drafting
LangGraph state machine; Instructor structured output; dbt compile gate; +30 changelog scenarios; guardrails.
Cut: skip backfill DAG.

### Week 5 — Baselines + benchmark final
B1, B2, B3 implemented; full benchmark on held-out 30%; confusion matrix; contamination canary.
Cut: B1 optional.

### Week 6 — Deploy, demo, launch
Fly.io deploy; 90s Loom; README final; blog draft; LinkedIn + thread queued; CI runs benchmark on PR.
Cut: skip hosted demo.

---

# DELIVERABLE 4 — README + BLOG + LAUNCH

## 4.1 README Outline
Hero metric line · GIF · architecture diagram · 60s how-it-works · results table · 3-command quickstart · honest limitations · `make bench`.

## 4.2 Blog Outline (~1500 words)
Pain story (3am rename) → why contracts/tests aren't enough → lineage insight → architecture → eval → what surprised me → roadmap.

Titles:
1. *The 3am Page That Doesn't Need to Happen: A Schema Drift Agent That Reads Your Lineage*
2. *Schema Drift Detective: Catching Breaking Changes Before Your Dashboards Do*
3. *Why dbt Tests Aren't Enough — and What I Built Instead*

## 4.3 LinkedIn Post
Drift takes down more dashboards than bad SQL → built an agent → 6 weeks → benchmark beats GE/dbt-tests/one-shot-LLM → most of it isn't an LLM → looking for senior/staff roles.

## 4.4 Tweet Thread (5 tweets)
1. Hook. 2. Classifying not preventing. 3. Most isn't LLM. 4. Benchmark. 5. Links + hiring CTA.

## 4.5 STAR Stories
- A: Where AI helps vs. where it's overkill (rules beat LLM on severity classification at 1/50th cost).
- B: Lineage as the unit of truth (dbt manifest + SQLGlot + Metabase API → impact recall 0.91 vs 0.62).
- C: Production guardrails on an agentic system (destructive-op gate, blast-radius cap, audit log, kill switch).
