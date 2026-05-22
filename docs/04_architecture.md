# Architecture (ADRs)

This document records the four load-bearing decisions in the agent.
Each section is an ADR: **context → decision → consequences**. The
order is deliberate — read top to bottom and you'll have the mental
model of the whole pipeline.

```
                ┌─────────────┐
  source ──────▶│  Watcher    │── SchemaSnapshot ─▶  SnapshotStore (DuckDB)
                └─────────────┘                            │
                                                            │ diff
                                                            ▼
                                              ┌──────────────────────┐
                                              │      Classifier      │  rule-only, deterministic
                                              └──────────────────────┘
                                                            │ DriftEvent
                                                            ▼
                                              ┌──────────────────────┐
                                              │   LineageGraph       │  manifest.json → column DAG
                                              └──────────────────────┘
                                                            │ ImpactSet
                                                            ▼
                                              ┌──────────────────────┐
                                              │   PolicyEngine       │  kill switch ▸ rate limit ▸ destructive ▸ blast
                                              └──────────────────────┘
                                                            │ Action
                                                  ┌─────────┼──────────┐
                                                  ▼         ▼          ▼
                                            Migrator     Slack       OpenLineage   (each no-op without config)
                                                  │
                                                  ▼ MigrationBundle (dbt files + tests + rollback)
                                            PR Gateway → GitHub
```

---

## ADR-1 — Rule classifier first, LLM only for drafting

**Context.** Schema drift is a 13-class classification problem with
**structural ground truth**: given a pre- and post-``ColumnSpec``, the
correct ``ChangeType`` is computable in closed form (e.g. "nullable
changed from True to False" ⇒ ``nullability_tightened``). The
classification recall ceiling on the benchmark is **1.000** for any
correct rule implementation; the one-shot LLM baseline ceilings at
**0.618** even with full snapshots in context.

**Decision.** Classification is deterministic Python. The LLM is only
on the **drafting** side: it proposes the migration SQL and updated
``schema.yml`` block, then a Pydantic validator (``MigrationProposal``
built per-event with ``Literal[allowed_columns]``) blocks any
hallucinated column names before they hit ``dbt parse``. The LLM
retries with the parse error in-loop, up to 2 retries.

**Consequences.**
* The hot path (detect + classify) is **80× faster** and **10×
  cheaper** than the LLM baseline, and bit-exactly reproducible in CI.
* The LLM **cannot** corrupt the classification — at worst it produces
  a bad draft that fails ``dbt parse`` and the agent gives up. The
  classifier verdict that ships in the audit log is always the rule's.
* When the LLM is unavailable (no API key, network down), the agent
  still produces a ``DriftEvent`` and a ``MigrationProposal``-less
  ``ALERT_ONLY`` action via the policy engine. Detection never
  depends on the LLM being up.

---

## ADR-2 — All policy logic in one pure module

**Context.** Earlier drafts had safety knobs scattered across the PR
Gateway (kill switch), the watcher (rate limit), the classifier
(destructive gate), and the migrator (auto-mergeable flag). When a
reviewer asked "what stops this thing if it goes berserk?", we had
to walk them through four files.

**Decision.** ``policy.py::PolicyEngine.decide(event, impact) →
PolicyDecision`` is the single chokepoint. Every gate — kill switch,
rate limit, destructive guard, blast-radius cap, severity flow — lives
in that one function, in that order. The engine is pure (state is just
a deque for the rate-limit window); side effects (Slack ping, audit
log) are the caller's job.

**Consequences.**
* Reading ``policy.py`` is the **single** prerequisite for trusting
  the agent. ~200 lines, no I/O, fully unit-tested as a state machine.
* The destructive-change gate is **double-locked**: ``DriftEvent``'s
  Pydantic validator refuses ``auto_mergeable=True`` for destructive
  change types, and the policy engine pins those to ``OPEN_DRAFT_PR``
  with a ``requires-human`` label. Either layer alone would catch the
  bug; both is belt-and-braces.
* The kill switch is an **env var**, not a config file, so an on-call
  can disarm the agent with ``DRIFT_KILL_SWITCH=1 systemctl restart``
  in seconds. No redeploy.

---

## ADR-3 — Column-level lineage from compiled SQL via SQLGlot

**Context.** dbt's ``manifest.json`` includes a coarse
``depends_on.nodes`` graph at the *model* level, but not column-level
edges. Without column lineage, "did changing ``orders.customer_id``
break ``mart_revenue_daily.customer_id``?" is unanswerable; we'd have
to widen blast radius to "every model downstream of orders", which
fires on every nullable add.

**Decision.** Parse the **compiled** SQL (``model.compiled_code``)
with **SQLGlot 25**, walk the AST to extract ``(model, column)``
projections, and stitch them into a ``DiGraph``. The walker handles:
- ``WITH`` CTEs (treated as transparent passthroughs by alias)
- ``JOIN`` (column carried through if SELECT references it)
- ``UNION ALL`` (one edge per union arm)
- ``SELECT *`` (set ``fan_out_conservative=True`` and union every
  upstream column — we widen rather than guess)

**Consequences.**
* ``ImpactSet.affected_columns`` returns the **transitive** column
  closure, not the model closure. On the test fixture the difference is
  4 columns vs 12 — a 3× false-positive reduction.
* ``fan_out_conservative`` is surfaced to the PR via a label
  (``fan-out-widened``) and to the policy engine (large blast → draft
  PR). Reviewers see "this was guessed because of SELECT *" rather
  than seeing a confident-looking-but-wrong impact list.
* The lineage code is the one place where we accept *probable*
  behaviour: lambdas in dbt macros, dialect-specific window functions,
  exotic JSON paths. The classifier and migrator are bit-exact; the
  lineage is best-effort with a confidence enum (``high|medium|low``).

---

## ADR-4 — Observability via three pluggable, no-op-by-default emitters

**Context.** A portfolio CI tool that demands a running Marquez,
Slack workspace, and Prometheus stack to even unit-test is dead on
arrival. But a tool that *can't* emit lineage, alerts, and metrics
isn't an enterprise candidate.

**Decision.** Three sibling modules — ``ol.py``, ``slack.py``,
``metrics.py`` — each:
1. Read configuration from one environment variable
   (``OPENLINEAGE_URL``, ``DRIFT_SLACK_WEBHOOK_URL``, no env needed
   for metrics — it's an in-memory ``CollectorRegistry`` until scraped).
2. **No-op when unset** — return ``False`` from ``emit/notify``,
   log nothing, raise nothing. This is the explicit contract; the
   unit tests in ``test_audit_ol_slack.py`` pin it.
3. **Swallow network errors** — a Marquez outage or a malformed
   Slack token must never break the drift pipeline. Errors are logged
   at ``WARNING`` and the call returns ``False``.
4. Accept an ``httpx.Client`` in the constructor so tests can inject
   a ``MockTransport`` and assert the JSON shape that hits the wire.

**Consequences.**
* CI runs **zero** external network calls. The unit suite for the
  three emitters mocks the transport.
* Bringing the agent up against a real Marquez + Slack + Prometheus
  is purely a matter of setting env vars; no code change.
* The audit log (``audit.py``, JSONL by default) is the **always-on**
  observability fallback. Even if all three emitters are disabled,
  every action lands in a JSONL file that an SRE can grep.
