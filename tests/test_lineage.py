"""Tests for ``LineageGraph`` (node-level forward BFS over dbt manifest.json)."""

from __future__ import annotations

from pathlib import Path

from schema_drift.lineage import LineageGraph

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"


class TestLineageGraph:
    def test_load_manifest(self):
        g = LineageGraph.from_manifest(FIXTURE)
        # 2 sources + 4 models + 1 exposure = 7 nodes
        assert g.graph.number_of_nodes() == 7
        # source->stg, source->stg, stg+stg->fct, fct->mart, mart->exposure
        assert g.graph.number_of_edges() >= 6

    def test_find_source_handles_trailing_column(self):
        g = LineageGraph.from_manifest(FIXTURE)
        node = g.find_source_node("source_raw.orders.discount_code")
        assert node == "source.drift_demo.source_raw.orders"

    def test_find_source_without_column(self):
        g = LineageGraph.from_manifest(FIXTURE)
        assert g.find_source_node("source_raw.customers") == (
            "source.drift_demo.source_raw.customers"
        )

    def test_impact_for_orders(self):
        g = LineageGraph.from_manifest(FIXTURE)
        impact = g.impact("source_raw.orders.discount_code")
        # orders → stg_orders → fct_orders → mart_revenue_daily (3 models downstream)
        # NOT stg_customers (different source).
        assert set(impact.dbt_models) == {"stg_orders", "fct_orders", "mart_revenue_daily"}
        # exposure adds 3.0 to the score.
        assert impact.blast_radius_score == 3.0 + 3.0
        assert impact.lineage_confidence == "high"

    def test_unknown_source_yields_low_confidence(self):
        g = LineageGraph.from_manifest(FIXTURE)
        impact = g.impact("nowhere.nope.x")
        assert impact.dbt_models == ()
        assert impact.lineage_confidence == "low"
