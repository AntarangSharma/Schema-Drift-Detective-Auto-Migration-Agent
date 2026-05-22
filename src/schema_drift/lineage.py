"""Lineage analysis from a dbt ``manifest.json``.

Two layers
----------
1. **Node-level** (Day 3) — forward BFS over the dbt DAG. Cheap, always works,
   used by the demo and the policy engine's blast-radius cap.
2. **Column-level** (Week 3) — parses each model's ``compiled_code`` with
   SQLGlot, builds a ``(model, column) → (model, column)`` DiGraph, and
   answers "if column ``X`` of source table ``S`` changes, which downstream
   model columns are affected?".

When SQLGlot can't resolve a projection (``SELECT *``, recursive CTEs we
don't model, dynamic SQL via Jinja), we widen *conservatively*: every
downstream column of the offending model is marked as potentially-affected
and ``ImpactSet.fan_out_conservative`` is set to ``True``.

dbt manifest conventions used here
----------------------------------
* Source unique IDs look like ``source.{project}.{source_name}.{table_name}``.
* Model unique IDs look like ``model.{project}.{model_name}``.
* Each node carries ``depends_on.nodes: list[str]`` and (when available)
  ``compiled_code: str``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import networkx as nx
import sqlglot
from sqlglot import exp

from schema_drift.models import ImpactSet

logger = logging.getLogger(__name__)


# A column-graph node is the tuple ``(node_id, column_name)``. We keep
# node_id (not table name) so the graph is unambiguous even when two
# sources share a table name across schemas.
ColNode = tuple[str, str]


class LineageGraph:
    """A directed graph of dbt nodes (sources, models, seeds, snapshots, tests).

    Edges point in the direction of data flow: ``upstream → downstream``.
    """

    def __init__(
        self,
        graph: nx.DiGraph | None = None,
        column_graph: nx.DiGraph | None = None,
        fan_out_models: frozenset[str] = frozenset(),
        model_columns: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.graph: nx.DiGraph = graph if graph is not None else nx.DiGraph()
        self.column_graph: nx.DiGraph = column_graph if column_graph is not None else nx.DiGraph()
        # Models we had to fan-out conservatively (``SELECT *`` or unparseable SQL).
        self.fan_out_models: frozenset[str] = fan_out_models
        # All projection columns we know per model — used to widen on fan-out.
        self.model_columns: dict[str, tuple[str, ...]] = model_columns or {}

    # ----------------------------------------------------------------- factory

    @classmethod
    def from_manifest(cls, manifest_path: str | Path) -> LineageGraph:
        """Build a node-level LineageGraph (no column graph) from a manifest."""
        data = json.loads(Path(manifest_path).read_text())
        g = _build_node_graph(data)
        return cls(g)

    @classmethod
    def from_manifest_with_columns(
        cls, manifest_path: str | Path, dialect: str = "postgres"
    ) -> LineageGraph:
        """Build node-level + column-level graphs in one pass.

        Each model's ``compiled_code`` is parsed with SQLGlot. For every
        top-level projection we record ``(upstream_node, upstream_col) →
        (this_node, output_col)`` edges in ``column_graph``. ``SELECT *``
        and unparseable SQL flip the model into the fan-out-conservative
        set so callers can widen impact safely.
        """
        data = json.loads(Path(manifest_path).read_text())
        node_graph = _build_node_graph(data)
        col_graph = nx.DiGraph()
        fan_out: set[str] = set()
        model_cols: dict[str, tuple[str, ...]] = {}

        # Index source columns onto the column graph so impact() seeds can match.
        for source_id, source in data.get("sources", {}).items():
            for col_name in _source_column_names(source):
                col_graph.add_node((source_id, col_name))

        # Walk model nodes.
        all_nodes = data.get("nodes", {})
        for node_id, node in all_nodes.items():
            if node.get("resource_type") != "model":
                continue
            depends_on = node.get("depends_on", {}).get("nodes", []) or []
            compiled = node.get("compiled_code") or node.get("raw_code") or ""
            try:
                cols, used_star = _extract_column_lineage(
                    compiled, node_id=node_id, depends_on=depends_on, dialect=dialect
                )
            except Exception as exc:
                logger.warning("column-lineage parse failed for %s: %s", node_id, exc)
                used_star = True
                cols = []

            if used_star:
                fan_out.add(node_id)

            output_cols: list[str] = []
            for out_col, sources in cols:
                tgt: ColNode = (node_id, out_col)
                col_graph.add_node(tgt)
                output_cols.append(out_col)
                for src_node_id, src_col in sources:
                    col_graph.add_node((src_node_id, src_col))
                    col_graph.add_edge((src_node_id, src_col), tgt)
            model_cols[node_id] = tuple(output_cols)

        return cls(
            graph=node_graph,
            column_graph=col_graph,
            fan_out_models=frozenset(fan_out),
            model_columns=model_cols,
        )

    # ------------------------------------------------------------------ query

    def find_source_node(self, source_identifier: str) -> str | None:
        """Locate the dbt source node matching ``schema.table[.column]``."""
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
        """Forward BFS from the matching source node. Node-level only."""
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
        # Whether any downstream model fanned-out conservatively.
        fan_out = bool(self.fan_out_models & descendants)
        return ImpactSet(
            dbt_models=tuple(models),
            blast_radius_score=1.0 * len(models) + 3.0 * len(exposures),
            lineage_confidence="high" if not fan_out else "medium",
            fan_out_conservative=fan_out,
        )

    def impact_columns(self, source_identifier: str, column: str) -> tuple[tuple[str, str], ...]:
        """Forward BFS over the column graph.

        Parameters
        ----------
        source_identifier
            ``"schema.table"`` (column suffix tolerated and stripped).
        column
            The source column name.

        Returns
        -------
        Sorted tuple of ``(model_name, column_name)`` pairs. Models that
        fan-out conservatively contribute *every* known projection column.
        """
        seed_node = self.find_source_node(source_identifier)
        if seed_node is None:
            return ()
        seed: ColNode = (seed_node, column)
        if not self.column_graph.has_node(seed):
            # Source column unknown — widen via node-level BFS.
            return self._widen_all_columns(seed_node)

        descendants: set[ColNode] = nx.descendants(self.column_graph, seed)  # type: ignore[assignment]

        # If any *node* on the dependency path is in fan_out_models, widen.
        descendants_node_ids: set[str] = {n for n, _ in descendants}
        node_level_descendants: set[str] = nx.descendants(self.graph, seed_node)  # type: ignore[assignment]
        fan_out_hit = self.fan_out_models & node_level_descendants
        widened: set[ColNode] = set(descendants)
        for fid in fan_out_hit:
            for col in self.model_columns.get(fid, ()):
                widened.add((fid, col))
        # Also propagate to *their* downstreams via node-level edges.
        for fid in fan_out_hit:
            for ds in nx.descendants(self.graph, fid):  # type: ignore[arg-type]
                for col in self.model_columns.get(ds, ()):
                    widened.add((ds, col))

        out: list[tuple[str, str]] = []
        for node_id, col in widened:
            attrs = self.graph.nodes.get(node_id, {})
            if attrs.get("kind") not in ("model", "exposure"):
                continue
            name = attrs.get("name", node_id)
            out.append((name, col))
        # ``descendants_node_ids`` is informational; reference here for log.
        logger.debug(
            "impact_columns(%s, %s) → %d cols across %d models (fan-out: %s)",
            source_identifier,
            column,
            len(out),
            len(descendants_node_ids),
            sorted(fan_out_hit),
        )
        return tuple(sorted(set(out)))

    def _widen_all_columns(self, source_node_id: str) -> tuple[tuple[str, str], ...]:
        """Fallback when the column-graph has no node for the seed: union
        every known projection column of every downstream model."""
        descendants: set[str] = nx.descendants(self.graph, source_node_id)  # type: ignore[assignment]
        out: list[tuple[str, str]] = []
        for ds in descendants:
            name = self.graph.nodes.get(ds, {}).get("name", ds)
            for col in self.model_columns.get(ds, ()):
                out.append((name, col))
        return tuple(sorted(set(out)))

    # ------------------------------------------------------------------- repr

    def __repr__(self) -> str:
        return (
            f"LineageGraph(nodes={self.graph.number_of_nodes()}, "
            f"edges={self.graph.number_of_edges()}, "
            f"col_nodes={self.column_graph.number_of_nodes()}, "
            f"fan_out={len(self.fan_out_models)})"
        )


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _build_node_graph(data: dict[str, Any]) -> nx.DiGraph:
    g = nx.DiGraph()
    for node_id, node in data.get("nodes", {}).items():
        g.add_node(
            node_id,
            kind=node.get("resource_type", "unknown"),
            name=node.get("name", node_id),
            schema=node.get("schema"),
            database=node.get("database"),
        )
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
    for exposure_id, exposure in data.get("exposures", {}).items():
        g.add_node(
            exposure_id,
            kind="exposure",
            name=exposure.get("name", exposure_id),
            exposure_type=exposure.get("type"),
        )
    for node_id, node in {
        **data.get("nodes", {}),
        **data.get("exposures", {}),
    }.items():
        for dep_id in node.get("depends_on", {}).get("nodes", []) or []:
            g.add_edge(dep_id, node_id)
    return g


def _source_column_names(source: dict[str, Any]) -> list[str]:
    """Extract declared column names from a dbt source dict.

    dbt manifest stores columns as ``{"columns": {"col_a": {...}, "col_b": {...}}}``.
    """
    cols = source.get("columns") or {}
    return list(cols.keys())


def _extract_column_lineage(
    sql: str,
    *,
    node_id: str,
    depends_on: list[str],
    dialect: str,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], bool]:
    """Parse one model's compiled SQL → list of ``(output_col, [(src_node_id, src_col), ...])``.

    Returns ``(projections, used_star)``. ``used_star`` is ``True`` if we
    encountered ``SELECT *``, ``SELECT t.*``, or any construct we don't model.

    Resolution strategy
    -------------------
    * Parse with SQLGlot.
    * Compute a CTE-name → set-of-projection-cols map and an alias → source
      map by walking ``FROM``/``JOIN`` clauses.
    * For each top-level ``Select`` projection:
        - ``*``           → mark used_star, return early.
        - ``alias.*``     → mark used_star (we don't expand columns yet).
        - ``alias.col``   → resolve alias → upstream node, attribute col.
        - ``col``         → attribute col to *every* upstream (unioned).
        - ``expr AS name``→ recurse into ``expr`` to collect column refs.
    * UNION ALL → recurse on each leg, merge projections positionally.
    """
    if not sql or not sql.strip():
        return [], True

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        raise

    return _extract_from_tree(tree, depends_on=depends_on)


def _extract_from_tree(
    tree: exp.Expression, *, depends_on: list[str]
) -> tuple[list[tuple[str, list[tuple[str, str]]]], bool]:
    # Strip enclosing parens.
    if isinstance(tree, exp.Paren):
        tree = tree.this  # type: ignore[assignment]

    # UNION ALL / UNION → merge legs positionally.
    if isinstance(tree, exp.Union):
        left_proj, left_star = _extract_from_tree(tree.this, depends_on=depends_on)
        right_proj, right_star = _extract_from_tree(tree.expression, depends_on=depends_on)
        if left_star or right_star:
            return [], True
        if len(left_proj) != len(right_proj):
            # Mis-shaped union — widen.
            return [], True
        merged: list[tuple[str, list[tuple[str, str]]]] = []
        for (name_l, srcs_l), (_, srcs_r) in zip(left_proj, right_proj, strict=False):
            merged.append((name_l, list({*srcs_l, *srcs_r})))
        return merged, False

    if not isinstance(tree, exp.Select):
        return [], True

    select: exp.Select = tree
    # Source resolution: alias → upstream node_id.
    alias_to_node: dict[str, str] = _resolve_aliases(select, depends_on=depends_on)
    cte_projections: dict[str, list[tuple[str, list[tuple[str, str]]]]] = _resolve_ctes(
        select, depends_on=depends_on
    )
    # If the outer FROM references a CTE (not an upstream model), record
    # which CTE backs each table-alias so unqualified columns can be
    # resolved via cte_projections.
    alias_to_cte: dict[str, str] = _resolve_cte_aliases(
        select, cte_names=set(cte_projections.keys())
    )

    projections: list[tuple[str, list[tuple[str, str]]]] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            return [], True

        # ``alias.*``
        if isinstance(projection, exp.Column) and projection.find(exp.Star):
            return [], True

        out_name = projection.alias_or_name or "_unnamed"
        col_refs = _collect_column_refs(
            projection,
            alias_to_node=alias_to_node,
            alias_to_cte=alias_to_cte,
            cte_projections=cte_projections,
        )
        # Deduplicate column refs.
        unique_refs = list({(n, c) for n, c in col_refs})
        projections.append((out_name, unique_refs))

    return projections, False


def _resolve_aliases(select: exp.Select, *, depends_on: list[str]) -> dict[str, str]:
    """Map *this Select's* immediate FROM/JOIN aliases to upstream node_ids.

    We intentionally do **not** ``find_all(exp.Table)``: that would pick up
    aliases nested inside CTEs and cause unqualified columns in the outer
    select to be attributed to every CTE-internal table. Instead we walk
    only the immediate ``FROM`` clause and ``JOIN``s of this Select.

    dbt's ``ref('x')`` / ``source('s','t')`` compile into
    ``database.schema.identifier``; we match by *trailing identifier*
    against the manifest's ``depends_on`` list, keeping us decoupled from
    schema conventions.
    """
    alias_map: dict[str, str] = {}
    tables: list[exp.Table] = []
    from_clause = select.args.get("from")
    if isinstance(from_clause, exp.From):
        tables.extend(t for t in from_clause.find_all(exp.Table))
    for join in select.args.get("joins", []) or []:
        if isinstance(join, exp.Join):
            tables.extend(t for t in join.find_all(exp.Table))
    for table in tables:
        alias = (table.alias_or_name or table.name).lower()
        upstream_id = _match_upstream(table.name, depends_on)
        if upstream_id is not None:
            alias_map[alias] = upstream_id
            alias_map[table.name.lower()] = upstream_id  # bare-name fallback
    return alias_map


def _resolve_ctes(
    select: exp.Select, *, depends_on: list[str]
) -> dict[str, list[tuple[str, list[tuple[str, str]]]]]:
    """Map CTE name → its inner projection list."""
    out: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {}
    with_clause = select.args.get("with")
    if with_clause is None:
        return out
    for cte in with_clause.expressions:
        name = cte.alias.lower()
        inner_select = cte.this
        proj, used_star = _extract_from_tree(inner_select, depends_on=depends_on)
        if used_star:
            out[name] = []  # caller will widen
        else:
            out[name] = proj
    return out


def _collect_column_refs(
    node: exp.Expression,
    *,
    alias_to_node: dict[str, str],
    alias_to_cte: dict[str, str],
    cte_projections: dict[str, list[tuple[str, list[tuple[str, str]]]]],
) -> list[tuple[str, str]]:
    """Walk an expression sub-tree collecting (upstream_node_id, col) refs.

    Resolution order, per column ref:

    1. ``alias.col`` where alias is a known upstream table → emit directly.
    2. ``alias.col`` where alias is a known CTE → look up the inner CTE
       projection list and inherit its sources.
    3. Unqualified ``col`` with a single CTE in FROM → look up that CTE.
    4. Unqualified ``col`` with one or more upstream aliases → attribute
       to every upstream (best-effort, possibly over-reporting but never
       silently dropping a downstream edge).
    """
    refs: list[tuple[str, str]] = []
    for col in node.find_all(exp.Column):
        col_name = col.name
        table_alias = (col.table or "").lower()

        # 1. Direct upstream alias.
        if table_alias and table_alias in alias_to_node:
            refs.append((alias_to_node[table_alias], col_name))
            continue
        # 2. CTE alias.
        if table_alias and table_alias in cte_projections:
            for proj_name, sources in cte_projections[table_alias]:
                if proj_name == col_name:
                    refs.extend(sources)
            continue
        # 3. Unqualified, exactly one CTE in scope via FROM.
        if not table_alias and alias_to_cte:
            for cte_name in set(alias_to_cte.values()):
                for proj_name, sources in cte_projections.get(cte_name, []):
                    if proj_name == col_name:
                        refs.extend(sources)
            if refs:
                continue
        # 4. Unqualified, multiple upstreams → attribute to every alias.
        if not table_alias and alias_to_node:
            for upstream in set(alias_to_node.values()):
                refs.append((upstream, col_name))
    return refs


def _resolve_cte_aliases(select: exp.Select, *, cte_names: set[str]) -> dict[str, str]:
    """Identify FROM/JOIN tables that reference a CTE rather than a real upstream."""
    out: dict[str, str] = {}
    tables: list[exp.Table] = []
    from_clause = select.args.get("from")
    if isinstance(from_clause, exp.From):
        tables.extend(from_clause.find_all(exp.Table))
    for join in select.args.get("joins", []) or []:
        if isinstance(join, exp.Join):
            tables.extend(join.find_all(exp.Table))
    for table in tables:
        name = table.name.lower()
        alias = (table.alias_or_name or table.name).lower()
        if name in cte_names:
            out[alias] = name
    return out


def _match_upstream(name: str, depends_on: list[str]) -> str | None:
    """Match a table reference (post-compile, may be schema-qualified) to
    one of the depends_on node IDs. We do this by the trailing segment so
    ``analytics.stg_orders`` and ``stg_orders`` both resolve to
    ``model.x.stg_orders``."""
    target = name.rsplit(".", maxsplit=1)[-1].lower()
    for dep_id in depends_on:
        if dep_id.split(".")[-1].lower() == target:
            return dep_id
    return None
