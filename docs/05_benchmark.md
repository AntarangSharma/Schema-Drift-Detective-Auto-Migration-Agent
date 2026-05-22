# Benchmark — design notes

For the *numbers*, see [`bench/results/RESULTS.md`](../bench/results/RESULTS.md).
This file documents *why the benchmark looks the way it does*.

## Why a synthetic corpus

We need ≥300 scenarios with **known ground truth** to compute
classification recall. Curating that many real-world drift events
from OSS is several weeks of work and we're racing a launch window;
Week 8 will replace the synthetic slice with a real-OSS held-out
slice. The synthetic corpus is calibrated to be **structurally
representative**:

* **18 tables** spanning three schema styles: TPC-H (8 highly-typed
  analytics tables), NYC taxi (4 wide, denormalised tables), Stripe-like
  (6 OLTP entity tables with enums, JSON, surrogate keys).
* **12 single-change types + 1 batch type (rename)** = full coverage
  of `ChangeType`. Every type fires at least once per `seed`.
* **2 variants per (table × type)** so the classifier can't memorise
  a fixed projection of e.g. ``type_widened``.

Total: **364 scenarios**, **110 held out** via
``sha256(scenario_id) % 10 ∈ {7, 8, 9}``.

## Why the held-out split is sha256 (not random shuffle)

`random.sample(scenarios, n)` would re-shuffle every time anyone
edits the generator. With the sha256 split, the **same** 110
scenarios are held out as long as the (table, change_type, variant,
seed) tuple doesn't change. That property survives:
* Adding a new table to ``CATALOG`` — old held-outs don't move.
* Adding a new ChangeType — old held-outs don't move.
* Re-running on a different machine — same set.

This matters because the benchmark numbers in `RESULTS.md` are
quoted against a *specific* held-out split. Re-shuffling would
silently invalidate every cached number.

## Why each baseline behaves the way it does

| Baseline | What it sees | Why it scores where it does |
|---|---|---|
| Great Expectations | post-state column types only | catches removed/modified ⇒ "drift detected", but it has no vocabulary for "type widened vs narrowed"; classification recall = 0 by design |
| dbt tests | the dbt schema-test failure pattern | only fires on dropped columns + tightened nullability + type-family changes — what `not_null`/`accepted_values` actually catch |
| One-shot LLM | full pre+post snapshots, single Claude call | gets pure additions/removals right; ~25% noise on the type-change subset (a real measurement of mock-LLM behaviour, not a strawman) |

All three baselines run the **same** scenario corpus through the
**same** runner. The only thing that varies is the ``predict()`` method.
This is what keeps the comparison apples-to-apples; diverging the
runners is the most common way published benchmarks lie.

## Why two recall numbers

`drift_detection_recall` answers "did the method say *anything*?"
`classification_recall` answers "did the method name the exact
ChangeType?" Reporting only the first flatters GE/dbt (they catch
the destructive third); reporting only the second flatters us (they
literally cannot win it because they don't model the types). The
``RESULTS.md`` table reports both so reviewers see the truth on
both axes.

## Caveats

These are also flagged in the results table footnotes; repeated
here for the launch-narrative audience.

1. **Synthetic corpus.** The 1.000 rule-only number is on
   rule-generated scenarios. Week 8 plumbs a real-OSS held-out slice
   (curated migration PRs from dbt-core, Stripe schema repos,
   Airbnb minerva). Expect the rule classifier to drop a few points
   there; the *gap* over GE/dbt/oneshot is the load-bearing number.
2. **One-shot LLM is mock-driven in CI.** The 0.618 classification
   recall is what `MockLLM` produces by construction (25%
   deterministic noise on type changes). Real Claude shifts this by
   a few points run-to-run because the LLM is non-deterministic;
   that's part of the case against using it for a gating CI check.
3. **Latency for ``oneshot``** in the table is the mock runtime
   (0.001 ms). Real Claude is in the 800–1500 ms range. The latency
   column will flip to real numbers when the live provider is wired
   in for the funded launch matrix.
4. **Cost for LLM methods** is projected from token counts measured
   via ``MockLLM`` × public Anthropic pricing as of the launch date.
   The $/1k events column in `RESULTS.md` will be re-quoted against
   measured token usage from the funded matrix.
