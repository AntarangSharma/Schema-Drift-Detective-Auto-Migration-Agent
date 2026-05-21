"""Lineage analysis from a dbt ``manifest.json``.

Day-3 scope: NODE-LEVEL forward BFS only.  Column-level lineage (SQLGlot-
based, with CTE/JOIN/UNION handling) is Week 3 — see
``docs/02_revised_plan.md``.

dbt manifest conventions used here
----------------------------------
* Source unique IDs look like ``source.{project}.{source_name}.{table_name}``.
* Model unique IDs look like ``model.{project}.{model_name}``.
* Each node carries ``depends_on.nodes: list[str]``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import networkx as nx

from schema_drift.models import ImpactSet


class LineageGraph:
    """A directed graph of dbt nodes (sources, models, seeds, snapshots, tests).

    Edges point in the direction of data flow: ``upstream → downstream``.
    """

    def __init__(self, graph: nx.DiGraph | None = None) -> None:
        self.graph: nx.DiGraph = graph if graph is not None else nx.DiGraph()

    # ----------------------------------------------------------------- factory

    @classmethod
    def from_manifest(cls, manifest_path: str | Path) -> LineageGraph:
        """Build a LineageGraph from a dbt-compiled ``manifest.json``."""
        data = json.loads(Path(manifest_path).read_text())
        g = nx.DiGraph()

        # Nodes (models, seeds, snapshots, tests, …).
        for node_id, node in data.get("nodes", {}).items():
            g.add_node(
                node_id,
                kind=node.get("resource_type", "unknown"),
                name=node.get("name", node_id),
                schema=node.get("schema"),
                database=node.get("database"),
            )

        # Sources (the seeds of every drift event we care about).
        for source_id, source in data.get("sources", {}).items():
            g.add_node(
                source_id,
                kind="source",
                name=source.get("name", source_id),
                identifier=source.get("identifier"),
                source_name=source.get("source_name"),
                schema=source.get("schema"),
                database=source.get("database"),
            )

        # Exposures (dashboards, reports). Treat as terminal downstreams.
        for exposure_id, exposure in data.get("exposures", {}).items():
            g.add_node(
                exposure_id,
                kind="exposure",
                name=exposure.get("name", exposure_id),
                exposure_type=exposure.get("type"),
            )

        # Edges from `depends_on.nodes`.
        for node_id, node in {
            **data.get("nodes", {}),
            **data.get("exposures", {}),
        }.items():
            for dep_id in node.get("depends_on", {}).get("nodes", []) or []:
                g.add_edge(dep_id, node_id)

        return cls(g)

    # ------------------------------------------------------------------ query

    def find_source_node(self, source_identifier: str) -> str | None:
        """Locate the dbt source node matching ``schema.table[.column]``.

        Accepts both ``"source_raw.orders"`` and ``"source_raw.orders.customer_id"``
        (the trailing column, if present, is stripped before matching).
        """
        # Strip an optional trailing column. Source identifiers in DriftEvents
        # carry the column; lineage matches on table.
        parts = source_identifier.split(".")
        schema, table = parts[0], parts[1] if len(parts) > 1 else None

        for node_id, attrs in self.graph.nodes(data=True):
            if attrs.get("kind") != "source":
                continue
            if attrs.get("schema") != schema:
                continue
            if table is not None and table not in (
                attrs.get("identifier"),
                attrs.get("name"),
            ):
                continue
            return node_id
        return None

    def impact(self, source_identifier: str) -> ImpactSet:
        """Forward BFS from the matching source node.

        Returns an ``ImpactSet`` populated with downstream dbt models and
        exposures. ``blast_radius_score`` is a simple weighted sum:
            score = 1.0 * |models| + 3.0 * |exposures|.
        """
        seed = self.find_source_node(source_identifier)
        if seed is None:
            return ImpactSet(lineage_confidence="low")

        descendants: set[str] = nx.descendants(self.graph, seed)  # type: ignore[assignment]

        models: list[str] = []
        exposures: list[str] = []
        for node_id in descendants:
            attrs: dict[str, Any] = self.graph.nodes[node_id]
            match attrs.get("kind"):
                case "model":
                    models.append(attrs.get("name", node_id))
                case "exposure":
                    exposures.append(attrs.get("name", node_id))

        models.sort()
        exposures.sort()
        return ImpactSet(
            dbt_models=tuple(models),
            blast_radius_score=1.0 * len(models) + 3.0 * len(exposures),
            lineage_confidence="high",
        )

    # ------------------------------------------------------------------- repr

    def __repr__(self) -> str:
        return f"LineageGraph(nodes={self.graph.number_of_nodes()}, edges={self.graph.number_of_edges()})"
