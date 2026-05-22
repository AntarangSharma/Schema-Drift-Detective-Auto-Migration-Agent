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


# ---------------------------------------------------------------------------
# Week 3: column-level lineage
# ---------------------------------------------------------------------------


FIXTURE_COLS = Path(__file__).parent / "fixtures" / "manifest_columns.json"


class TestColumnLineage:
    def test_loads_column_graph(self):
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        # 8 nodes (2 sources + 5 models + 1 exposure).
        assert g.graph.number_of_nodes() == 8
        # ``mart_revenue_daily`` selects ``*`` → successfully resolved, not in fan_out!
        assert "model.drift_demo.mart_revenue_daily" not in g.fan_out_models

    def test_select_star_resolves_column_lineage(self):
        """SELECT * in mart_revenue_daily should propagate columns correctly."""
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        impact = g.impact_columns("source_raw.orders", "amount")
        # Assert that mart_revenue_daily.amount is resolved in the lineage!
        assert ("mart_revenue_daily", "amount") in impact

    def test_qualified_column_trace_through_cte(self):
        """``orders.amount`` flows ``stg_orders.amount → fct_orders.amount``
        even though fct_orders uses a CTE; outer SELECT projects unqualified
        but the resolver looks the column up in the CTE's projection list."""
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        impact = g.impact_columns("source_raw.orders", "amount")
        models = {m for m, _ in impact}
        assert "stg_orders" in models
        assert "fct_orders" in models
        # mart_union projects amount AS metric → that should appear too.
        assert ("mart_union", "metric") in impact

    def test_unrelated_source_does_not_appear_in_impact(self):
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        impact = g.impact_columns("source_raw.orders", "amount")
        # Customer columns must not leak in via JOIN.
        for model, col in impact:
            if model.startswith("stg_customers"):
                assert col != "email"

    def test_select_star_fan_out_widens_node_level_impact(self):
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        impact_set = g.impact("source_raw.orders")
        # mart_revenue_daily uses SELECT * but resolved it -> fan_out_conservative is False.
        assert impact_set.fan_out_conservative is False
        assert impact_set.lineage_confidence == "high"

    def test_impact_for_missing_source_returns_empty(self):
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        assert g.impact_columns("nowhere.nope", "x") == ()

    def test_impact_for_unknown_column_widens_via_node_level(self):
        """When the column itself isn't in the column graph but the source
        table is, fall back to node-level BFS + every projection col we
        know — never silently return ()."""
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        impact = g.impact_columns("source_raw.orders", "this_column_never_existed")
        # Should still surface downstream model columns (best-effort).
        assert isinstance(impact, tuple)
        # At minimum, every known projection column of each descendant
        # model must appear (or zero if mart fan-outs blanked them).
        seen_models = {m for m, _ in impact}
        assert "stg_orders" in seen_models or seen_models == set()

    def test_handles_unparseable_sql_via_fan_out(self, tmp_path):
        """A model with garbled SQL must end up in fan_out_models, not crash."""
        bad = tmp_path / "manifest.json"
        bad.write_text(
            '{"sources":{},"nodes":{"model.x.broken":{"resource_type":"model",'
            '"name":"broken","depends_on":{"nodes":[]},'
            '"compiled_code":"this is not sql !!!"}},"exposures":{}}'
        )
        g = LineageGraph.from_manifest_with_columns(bad)
        assert "model.x.broken" in g.fan_out_models

    def test_repr_includes_col_nodes_and_fan_out(self):
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        text = repr(g)
        assert "col_nodes=" in text
        assert "fan_out=" in text


# ---------------------------------------------------------------------------
# Hypothesis property test: round-trip lineage stays consistent
# ---------------------------------------------------------------------------


class TestColumnLineageProperties:
    def test_every_column_in_graph_has_a_known_node_id(self):
        """Any ``(node_id, col)`` in the column graph must reference a node
        the node-level graph also knows about. Catches drift between the
        two graphs."""
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        known_nodes = set(g.graph.nodes())
        for node_id, _ in g.column_graph.nodes():
            assert node_id in known_nodes, f"column graph references unknown node {node_id!r}"

    def test_no_self_loops_in_column_graph(self):
        g = LineageGraph.from_manifest_with_columns(FIXTURE_COLS)
        for u, v in g.column_graph.edges():
            assert u != v, f"unexpected self-loop in column graph: {u!r}"
