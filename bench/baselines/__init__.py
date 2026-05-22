"""Baseline methods for the benchmark.

Each baseline is a class that implements ``predict(raws) → (change_type,
severity)`` so the runner can score it through the same harness as our
classifier. The harness, held-out split, and metric definitions stay
identical across methods — that is what makes the final RESULTS table
honest.

Three baselines today:

* ``GreatExpectationsBaseline`` — runs GE expectations against the
  post-change snapshot. Stubbed if GE isn't importable.
* ``DbtTestsBaseline`` — replays drift into the source's dbt project and
  runs ``dbt test``. Stubbed if dbt isn't on PATH.
* ``OneShotLLMBaseline`` — single LLM call with pre+post snapshots in
  the prompt; expects the model to emit ``ChangeType``. Uses ``MockLLM``
  in CI.
"""

from bench.baselines.dbt_tests_baseline import DbtTestsBaseline
from bench.baselines.ge_baseline import GreatExpectationsBaseline
from bench.baselines.one_shot_llm_baseline import OneShotLLMBaseline

__all__ = ["DbtTestsBaseline", "GreatExpectationsBaseline", "OneShotLLMBaseline"]
