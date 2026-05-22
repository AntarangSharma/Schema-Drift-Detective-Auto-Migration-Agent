"""Tests for the bench generator + runner.

We don't re-test classifier correctness here (that's
``tests/test_classifier.py``). We test that:

* generation is deterministic for a fixed seed,
* every scenario round-trips through Pydantic without loss,
* the runner glue can replay scenarios end-to-end and yield perfect
  scores against the rule classifier,
* the held-out split is stable across runs.
"""

from __future__ import annotations

from pathlib import Path

from bench.generate import (
    CATALOG,
    SINGLE_CHANGE_TYPES,
    generate_all,
    held_out,
    write_scenarios,
)
from bench.runner import run

from schema_drift.models import ChangeType, RawChange


class TestGenerator:
    def test_generates_at_least_300_scenarios_with_default_variants(self):
        scenarios = generate_all(seed=20260101, variants=2)
        assert len(scenarios) >= 300

    def test_catalog_covers_18_tables(self):
        assert len(CATALOG) == 18

    def test_emits_all_single_change_types_overall(self):
        scenarios = generate_all(seed=42, variants=2)
        emitted = {s.expected_change_type for s in scenarios}
        # Every single-change ChangeType must appear at least once across
        # the 18-table catalog (otherwise the rule has zero coverage).
        for ct in SINGLE_CHANGE_TYPES:
            assert ct.value in emitted, f"{ct.value} missing from corpus"
        # Rename pair must also be present.
        assert ChangeType.COLUMN_RENAMED.value in emitted

    def test_determinism_same_seed_same_output(self):
        a = generate_all(seed=7, variants=2)
        b = generate_all(seed=7, variants=2)
        assert [s.scenario_id for s in a] == [s.scenario_id for s in b]

    def test_different_seed_different_ids(self):
        a = {s.scenario_id for s in generate_all(seed=7, variants=2)}
        b = {s.scenario_id for s in generate_all(seed=8, variants=2)}
        # IDs hash the seed, so they must diverge.
        assert a != b

    def test_held_out_split_is_stable(self):
        scenarios = generate_all(seed=20260101, variants=2)
        first = [held_out(s.scenario_id) for s in scenarios]
        second = [held_out(s.scenario_id) for s in scenarios]
        assert first == second
        # And it should pick out roughly 30% of scenarios.
        ratio = sum(first) / len(first)
        assert 0.20 < ratio < 0.40

    def test_raw_changes_round_trip_through_pydantic(self):
        scenarios = generate_all(seed=20260101, variants=1)
        for s in scenarios:
            for r in s.raw_changes:
                # Validate via Pydantic — any drift here is a generator bug.
                RawChange(**r)


class TestRunner:
    def test_ours_method_round_trip(self, tmp_path: Path):
        scenarios = generate_all(seed=20260101, variants=1)
        write_scenarios(scenarios, tmp_path / "scenarios")
        summary = run(
            method="ours",
            scenarios_dir=tmp_path / "scenarios",
            results_dir=tmp_path / "results",
        )
        # Synthetic corpus from our own rules ⇒ perfect rule-only score.
        assert summary.detection_recall == 1.0
        assert summary.detection_precision == 1.0
        # Result file was written.
        assert (tmp_path / "results" / "results-ours.json").exists()

    def test_baselines_dont_crash_and_score_below_ours(self, tmp_path: Path):
        """All three baselines must run on the full corpus. GE and dbt
        return ``"unknown"`` (drift detected, type unknown) so
        ``classification_recall=0`` is *expected*; ``oneshot`` actually
        guesses change_types so it gets some right. None should beat us.
        """
        scenarios = generate_all(seed=20260101, variants=1)
        write_scenarios(scenarios, tmp_path / "scenarios")
        ours = run(
            method="ours",
            scenarios_dir=tmp_path / "scenarios",
            results_dir=tmp_path / "results",
        )
        for m in ("ge", "dbt", "oneshot"):
            summary = run(
                method=m,  # type: ignore[arg-type]
                scenarios_dir=tmp_path / "scenarios",
                results_dir=tmp_path / "results",
            )
            assert summary.total > 0
            # No baseline should out-classify us.
            assert summary.classification_recall <= ours.classification_recall
            # GE / dbt return "unknown" so classification recall is 0.
            # OneShot guesses something for every scenario.
            if m in ("ge", "dbt"):
                assert summary.classification_recall == 0.0
                # …but they DO detect drift on destructive change types.
                assert summary.drift_detection_recall > 0.0
            else:  # oneshot
                assert 0.0 < summary.classification_recall < 1.0
