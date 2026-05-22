"""Benchmark runner.

Loads scenario JSONs from ``bench/scenarios/`` and replays them against a
chosen *method*. Methods registered today:

* ``ours``     — our deterministic ``Classifier`` (the rule-only path).
* ``ge``       — Great Expectations baseline (stub; Week 5 wires real GE).
* ``dbt``      — dbt-tests baseline (stub; Week 5 wires real dbt).
* ``oneshot``  — one-shot LLM baseline (stub via ``MockLLM`` in CI).

Outputs
-------
* ``bench/results/results.json`` — per-scenario predictions + aggregate
  metrics (detection precision/recall, severity macro-F1, mean time).
* Pretty-printed summary table to stdout.

Why one runner for all methods
------------------------------
A single harness keeps the held-out split, scenario corpus, and metric
definitions identical across baselines. Diverging would let a methods
quietly compare apples to oranges in the final RESULTS table.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from bench.baselines import (
    DbtTestsBaseline,
    GreatExpectationsBaseline,
    OneShotLLMBaseline,
)
from bench.generate import held_out
from schema_drift.classifier import Classifier
from schema_drift.models import RawChange

Method = Literal["ours", "ge", "dbt", "oneshot"]


# ---------------------------------------------------------------------------
# Method protocol
# ---------------------------------------------------------------------------


class BaselineMethod(Protocol):
    """A scoring method: takes a list of ``RawChange`` and returns the
    predicted ``ChangeType`` value (string) and severity, or ``None``
    if the method cannot decide."""

    def predict(self, raws: list[RawChange]) -> tuple[str | None, str | None]: ...


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


class OursMethod:
    """Our rule-only classifier (the headline number in the README)."""

    def __init__(self) -> None:
        self._clf = Classifier()

    def predict(self, raws: list[RawChange]) -> tuple[str | None, str | None]:
        events = self._clf.classify_batch(raws)
        if not events:
            return None, None
        # If batch yields multiple events for one scenario, take the
        # highest-severity one (mirrors how the orchestrator picks).
        events.sort(key=lambda e: e.severity.rank, reverse=True)
        return events[0].change_type.value, events[0].severity.value


def make_method(method: Method) -> BaselineMethod:
    """Construct the scoring method.

    Each baseline lives in its own module (``bench/baselines/*.py``) so
    the runner stays thin and the baselines can grow independently
    without bloating this file.
    """
    match method:
        case "ours":
            return OursMethod()
        case "ge":
            return GreatExpectationsBaseline()
        case "dbt":
            return DbtTestsBaseline()
        case "oneshot":
            return OneShotLLMBaseline()
    raise ValueError(f"Unknown method: {method!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScenarioResult:
    scenario_id: str
    expected_change_type: str
    expected_severity: str
    predicted_change_type: str | None
    predicted_severity: str | None
    held_out: bool
    elapsed_ms: float
    correct_change_type: bool
    correct_severity: bool


@dataclass(slots=True)
class RunSummary:
    method: str
    seed: int
    total: int
    held_out_count: int
    # Did the method fire on drift at all (true positive = any prediction)?
    drift_detection_recall: float
    # Did the method get the exact ChangeType right? (strict)
    classification_recall: float
    classification_precision: float
    severity_macro_f1: float
    mean_latency_ms: float
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)

    # Backwards-compat aliases so older callers / tests / RESULTS readers
    # still see the original names. We keep both because the metric the
    # README reports (classification_recall) is the strict one, but the
    # honest comparison against GE/dbt has to use the loose one.
    @property
    def detection_recall(self) -> float:
        return self.classification_recall

    @property
    def detection_precision(self) -> float:
        return self.classification_precision


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _macro_f1(per_class: dict[str, dict[str, int]]) -> float:
    """Compute macro-F1 from a per-class confusion stub ``{class: {tp,fp,fn}}``."""
    f1s: list[float] = []
    for stats in per_class.values():
        tp = stats.get("tp", 0)
        fp = stats.get("fp", 0)
        fn = stats.get("fn", 0)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        if precision + recall == 0:
            f1s.append(0.0)
        else:
            f1s.append(2 * precision * recall / (precision + recall))
    return statistics.mean(f1s) if f1s else 0.0


def evaluate(
    results: list[ScenarioResult],
) -> tuple[float, float, float, float, dict[str, dict[str, int]]]:
    """Compute drift-detection recall + classification R/P + severity
    macro-F1 + confusion matrix.

    Returns ``(drift_recall, classification_recall, classification_precision,
    sev_f1, confusion)``.

    Two recalls
    -----------
    * ``drift_recall`` = % of scenarios where the method predicted
      *anything* (i.e. detected drift). GE / dbt typically score well
      here even when they can't name the ChangeType.
    * ``classification_recall`` = % where the method named the exact
      ChangeType. The strict bar; our rule classifier is the only method
      that's allowed to claim a high number here.
    """
    total = len(results)
    detected = sum(1 for r in results if r.predicted_change_type is not None)
    correct = sum(1 for r in results if r.correct_change_type)
    drift_recall = detected / total if total else 0.0
    classification_recall = correct / total if total else 0.0
    classification_precision = correct / detected if detected else 0.0

    # Confusion + per-severity F1.
    confusion: dict[str, dict[str, int]] = {}
    per_severity: dict[str, dict[str, int]] = {}
    for r in results:
        exp = r.expected_change_type
        pred = r.predicted_change_type or "<none>"
        confusion.setdefault(exp, Counter())[pred] += 1  # type: ignore[arg-type]

        sev_exp = r.expected_severity
        sev_pred = r.predicted_severity or "<none>"
        stats = per_severity.setdefault(sev_exp, {"tp": 0, "fp": 0, "fn": 0})
        if sev_pred == sev_exp:
            stats["tp"] += 1
        else:
            stats["fn"] += 1
            per_severity.setdefault(sev_pred, {"tp": 0, "fp": 0, "fn": 0})["fp"] += 1

    # Normalise Counters → dicts for JSON.
    confusion = {k: dict(v) for k, v in confusion.items()}
    return (
        drift_recall,
        classification_recall,
        classification_precision,
        _macro_f1(per_severity),
        confusion,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _load_scenarios(scenarios_dir: Path) -> list[dict[str, Any]]:
    paths = sorted(scenarios_dir.glob("*.json"))
    return [json.loads(p.read_text()) for p in paths]


def _raws_from_scenario(s: dict[str, Any]) -> list[RawChange]:
    return [RawChange(**r) for r in s["raw_changes"]]


def run(
    method: Method,
    scenarios_dir: Path,
    results_dir: Path,
    only_held_out: bool = False,
) -> RunSummary:
    impl = make_method(method)
    scenarios = _load_scenarios(scenarios_dir)
    if only_held_out:
        scenarios = [s for s in scenarios if held_out(s["scenario_id"])]

    results: list[ScenarioResult] = []
    for s in scenarios:
        raws = _raws_from_scenario(s)
        t0 = time.perf_counter()
        pred_ct, pred_sev = impl.predict(raws)
        elapsed = (time.perf_counter() - t0) * 1_000
        results.append(
            ScenarioResult(
                scenario_id=s["scenario_id"],
                expected_change_type=s["expected_change_type"],
                expected_severity=s["expected_severity"],
                predicted_change_type=pred_ct,
                predicted_severity=pred_sev,
                held_out=held_out(s["scenario_id"]),
                elapsed_ms=elapsed,
                correct_change_type=pred_ct == s["expected_change_type"],
                correct_severity=pred_sev == s["expected_severity"],
            )
        )

    drift_recall, cls_recall, cls_precision, sev_f1, confusion = evaluate(results)
    summary = RunSummary(
        method=method,
        seed=0,
        total=len(results),
        held_out_count=sum(1 for r in results if r.held_out),
        drift_detection_recall=drift_recall,
        classification_recall=cls_recall,
        classification_precision=cls_precision,
        severity_macro_f1=sev_f1,
        mean_latency_ms=statistics.mean(r.elapsed_ms for r in results) if results else 0.0,
        confusion=confusion,
    )

    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"results-{method}.json"
    payload = {
        "summary": asdict(summary),
        "results": [asdict(r) for r in results],
    }
    out_path.write_text(json.dumps(payload, sort_keys=True, indent=2))

    return summary


def _print_summary(summary: RunSummary) -> None:
    print(f"\n=== {summary.method!r} bench summary ({summary.total} scenarios) ===")
    print(f"  drift-detected (any)    : {summary.drift_detection_recall:.3f}")
    print(f"  classification recall   : {summary.classification_recall:.3f}")
    print(f"  classification precision: {summary.classification_precision:.3f}")
    print(f"  severity macro-F1       : {summary.severity_macro_f1:.3f}")
    print(f"  mean latency            : {summary.mean_latency_ms:.3f} ms")
    print(f"  held-out scenarios      : {summary.held_out_count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the schema-drift benchmark.")
    p.add_argument("--method", choices=("ours", "ge", "dbt", "oneshot"), default="ours")
    p.add_argument(
        "--scenarios",
        type=Path,
        default=Path(__file__).parent / "scenarios",
    )
    p.add_argument(
        "--results",
        type=Path,
        default=Path(__file__).parent / "results",
    )
    p.add_argument(
        "--held-out-only",
        action="store_true",
        help="Evaluate only the held-out split (W5 final number).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    summary = run(args.method, args.scenarios, args.results, only_held_out=args.held_out_only)
    _print_summary(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
