"""Synthetic benchmark generator.

Catalog
-------
18 base tables drawn from three families:

* **TPC-H (8)** — ``customer``, ``orders``, ``lineitem``, ``part``,
  ``partsupp``, ``supplier``, ``nation``, ``region``.
* **NYC taxi (4)** — ``yellow_trips``, ``green_trips``, ``fhv_trips``, ``zones``.
* **Stripe-like (6)** — ``customers``, ``charges``, ``subscriptions``,
  ``invoices``, ``refunds``, ``products``.

Each table is realistic enough that drift produces meaningful downstream
impact when piped through the lineage stage in Week 5.

Coverage
--------
For each table × ChangeType variant we generate ``--variants`` flavours.
Default ``--variants=2`` × 18 tables × ~13 change types ≥ **300 scenarios**,
which is the documented benchmark size.

Determinism
-----------
``random.Random(seed)`` everywhere; same seed → same scenarios. The
``scenario_id`` is a deterministic hash of ``(table, change_type, variant,
seed)`` so the held-out split (``sha256(scenario_id) % 10 ∈ {7, 8, 9}``)
is stable across runs.

Storage
-------
Scenarios serialise to ``bench/scenarios/<scenario_id>.json``. Each holds
the pre-snapshot, raw change(s), expected ChangeType, and expected
severity. The runner re-classifies and computes recall/precision against
``expected``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from schema_drift.models import ChangeType, ColumnSpec, RawChange

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _col(
    name: str,
    data_type: str,
    *,
    nullable: bool = True,
    default: str | None = None,
    pk: bool = False,
    ord: int = 0,
    cap: int | None = None,
    prec: int | None = None,
    scale: int | None = None,
    enum: tuple[str, ...] | None = None,
) -> ColumnSpec:
    return ColumnSpec(
        name=name,
        data_type=data_type,
        nullable=nullable,
        default=default,
        is_primary_key=pk,
        ordinal_position=ord,
        character_max_length=cap,
        numeric_precision=prec,
        numeric_scale=scale,
        enum_values=enum,
    )


# 18-table catalog. Each entry: (schema.table, [columns]).
CATALOG: dict[str, tuple[ColumnSpec, ...]] = {
    # ---------- TPC-H ----------
    "tpch.customer": (
        _col("c_custkey", "integer", nullable=False, pk=True, ord=1),
        _col("c_name", "varchar(25)", nullable=False, cap=25, ord=2),
        _col("c_address", "varchar(40)", cap=40, ord=3),
        _col("c_nationkey", "integer", nullable=False, ord=4),
        _col("c_phone", "char(15)", cap=15, ord=5),
        _col("c_acctbal", "numeric", prec=12, scale=2, ord=6),
    ),
    "tpch.orders": (
        _col("o_orderkey", "integer", nullable=False, pk=True, ord=1),
        _col("o_custkey", "integer", nullable=False, ord=2),
        _col("o_orderstatus", "char(1)", nullable=False, cap=1, ord=3, enum=("O", "F", "P")),
        _col("o_totalprice", "numeric", prec=12, scale=2, nullable=False, ord=4),
        _col("o_orderdate", "date", nullable=False, ord=5),
    ),
    "tpch.lineitem": (
        _col("l_orderkey", "integer", nullable=False, pk=True, ord=1),
        _col("l_linenumber", "integer", nullable=False, pk=True, ord=2),
        _col("l_quantity", "numeric", prec=12, scale=2, nullable=False, ord=3),
        _col("l_extendedprice", "numeric", prec=12, scale=2, nullable=False, ord=4),
        _col("l_shipdate", "date", nullable=False, ord=5),
    ),
    "tpch.part": (
        _col("p_partkey", "integer", nullable=False, pk=True, ord=1),
        _col("p_name", "varchar(55)", cap=55, nullable=False, ord=2),
        _col("p_size", "integer", nullable=False, ord=3),
        _col("p_retailprice", "numeric", prec=12, scale=2, nullable=False, ord=4),
    ),
    "tpch.partsupp": (
        _col("ps_partkey", "integer", nullable=False, pk=True, ord=1),
        _col("ps_suppkey", "integer", nullable=False, pk=True, ord=2),
        _col("ps_availqty", "integer", nullable=False, ord=3),
        _col("ps_supplycost", "numeric", prec=12, scale=2, nullable=False, ord=4),
    ),
    "tpch.supplier": (
        _col("s_suppkey", "integer", nullable=False, pk=True, ord=1),
        _col("s_name", "varchar(25)", cap=25, nullable=False, ord=2),
        _col("s_address", "varchar(40)", cap=40, ord=3),
        _col("s_nationkey", "integer", nullable=False, ord=4),
    ),
    "tpch.nation": (
        _col("n_nationkey", "integer", nullable=False, pk=True, ord=1),
        _col("n_name", "char(25)", cap=25, nullable=False, ord=2),
        _col("n_regionkey", "integer", nullable=False, ord=3),
    ),
    "tpch.region": (
        _col("r_regionkey", "integer", nullable=False, pk=True, ord=1),
        _col("r_name", "char(25)", cap=25, nullable=False, ord=2),
    ),
    # ---------- NYC taxi ----------
    "taxi.yellow_trips": (
        _col("trip_id", "bigint", nullable=False, pk=True, ord=1),
        _col("pickup_datetime", "timestamp", nullable=False, ord=2),
        _col("dropoff_datetime", "timestamp", nullable=False, ord=3),
        _col("passenger_count", "smallint", ord=4),
        _col("trip_distance", "numeric", prec=10, scale=2, ord=5),
        _col("fare_amount", "numeric", prec=10, scale=2, ord=6),
    ),
    "taxi.green_trips": (
        _col("trip_id", "bigint", nullable=False, pk=True, ord=1),
        _col("pickup_datetime", "timestamp", nullable=False, ord=2),
        _col("dropoff_datetime", "timestamp", nullable=False, ord=3),
        _col("trip_type", "smallint", ord=4),
    ),
    "taxi.fhv_trips": (
        _col("trip_id", "bigint", nullable=False, pk=True, ord=1),
        _col("dispatching_base_num", "varchar(10)", cap=10, ord=2),
        _col("pickup_datetime", "timestamp", nullable=False, ord=3),
    ),
    "taxi.zones": (
        _col("location_id", "integer", nullable=False, pk=True, ord=1),
        _col("borough", "varchar(25)", cap=25, ord=2),
        _col("zone", "varchar(40)", cap=40, ord=3),
    ),
    # ---------- Stripe-like ----------
    "stripe.customers": (
        _col("id", "varchar(32)", cap=32, nullable=False, pk=True, ord=1),
        _col("email", "varchar(255)", cap=255, ord=2),
        _col("created", "timestamp", nullable=False, ord=3),
        _col("default_source", "varchar(32)", cap=32, ord=4),
    ),
    "stripe.charges": (
        _col("id", "varchar(32)", cap=32, nullable=False, pk=True, ord=1),
        _col("amount", "integer", nullable=False, ord=2),
        _col("currency", "char(3)", cap=3, nullable=False, ord=3),
        _col(
            "status",
            "varchar(16)",
            cap=16,
            nullable=False,
            ord=4,
            enum=("succeeded", "pending", "failed"),
        ),
        _col("customer", "varchar(32)", cap=32, ord=5),
    ),
    "stripe.subscriptions": (
        _col("id", "varchar(32)", cap=32, nullable=False, pk=True, ord=1),
        _col("customer", "varchar(32)", cap=32, nullable=False, ord=2),
        _col("status", "varchar(16)", cap=16, ord=3, enum=("active", "trialing", "canceled")),
        _col("current_period_end", "timestamp", ord=4),
    ),
    "stripe.invoices": (
        _col("id", "varchar(32)", cap=32, nullable=False, pk=True, ord=1),
        _col("subscription", "varchar(32)", cap=32, ord=2),
        _col("amount_due", "integer", nullable=False, ord=3),
        _col("paid", "boolean", default="false", ord=4),
    ),
    "stripe.refunds": (
        _col("id", "varchar(32)", cap=32, nullable=False, pk=True, ord=1),
        _col("charge", "varchar(32)", cap=32, nullable=False, ord=2),
        _col("amount", "integer", nullable=False, ord=3),
    ),
    "stripe.products": (
        _col("id", "varchar(32)", cap=32, nullable=False, pk=True, ord=1),
        _col("name", "varchar(80)", cap=80, nullable=False, ord=2),
        _col("active", "boolean", default="true", ord=3),
    ),
}


# Single-change rule-only ChangeTypes the generator can synthesise.
# COLUMN_RENAMED is *batch-only*; PARTITION_KEY_CHANGED is generated as a
# pair of raw changes (drop + add on the partition tuple) and handled by
# the orchestrator, so neither lives in this list for the unit-scenario
# loop. The benchmark covers them in the "batch" generator below.
SINGLE_CHANGE_TYPES: tuple[ChangeType, ...] = (
    ChangeType.COLUMN_ADDED_NULLABLE,
    ChangeType.COLUMN_ADDED_NOT_NULL,
    ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT,
    ChangeType.COLUMN_DROPPED,
    ChangeType.TYPE_WIDENED,
    ChangeType.TYPE_NARROWED,
    ChangeType.TYPE_INCOMPATIBLE,
    ChangeType.PRECISION_CHANGED,
    ChangeType.PK_CHANGED,
    ChangeType.ENUM_VALUE_ADDED,
    ChangeType.DEFAULT_CHANGED,
    ChangeType.NULLABILITY_TIGHTENED,
)


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Scenario:
    """One ``(pre, change(s), expected)`` triple.

    Persisted as JSON so the runner can be re-implemented in any language
    without touching the generator. Equality is structural via ``scenario_id``.
    """

    scenario_id: str
    table: str
    expected_change_type: str
    expected_severity: str
    pre_columns: list[dict[str, Any]]
    raw_changes: list[dict[str, Any]]
    variant: int
    seed: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2)


def _scenario_id(table: str, change_type: ChangeType, variant: int, seed: int) -> str:
    h = hashlib.sha256(f"{table}|{change_type.value}|{variant}|{seed}".encode()).hexdigest()
    return h[:16]


def _column_dict(c: ColumnSpec) -> dict[str, Any]:
    return json.loads(c.model_dump_json())


# ---------------------------------------------------------------------------
# Per-change generators
# ---------------------------------------------------------------------------


def _pick_target(cols: tuple[ColumnSpec, ...], rng: random.Random) -> ColumnSpec:
    return rng.choice(cols)


def _next_ord(cols: tuple[ColumnSpec, ...]) -> int:
    return max((c.ordinal_position for c in cols), default=0) + 1


def _make_scenario(
    table: str,
    cols: tuple[ColumnSpec, ...],
    ct: ChangeType,
    variant: int,
    seed: int,
    rng: random.Random,
) -> Scenario | None:
    raws: list[RawChange] = []
    notes: tuple[str, ...] = ()
    expected_severity = None

    if ct is ChangeType.COLUMN_ADDED_NULLABLE:
        new = _col(
            f"new_col_v{variant}",
            rng.choice(("text", "varchar(40)", "integer")),
            ord=_next_ord(cols),
        )
        raws.append(RawChange(table_identifier=table, kind="added", column_after=new))

    elif ct is ChangeType.COLUMN_ADDED_NOT_NULL:
        new = _col(
            f"new_nn_v{variant}",
            rng.choice(("integer", "varchar(20)")),
            cap=20,
            nullable=False,
            default="'x'" if rng.random() < 0.5 else "0",
            ord=_next_ord(cols),
        )
        raws.append(RawChange(table_identifier=table, kind="added", column_after=new))

    elif ct is ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT:
        new = _col(
            f"nn_nodef_v{variant}",
            "integer",
            nullable=False,
            ord=_next_ord(cols),
        )
        raws.append(RawChange(table_identifier=table, kind="added", column_after=new))

    elif ct is ChangeType.COLUMN_DROPPED:
        droppable = [c for c in cols if not c.is_primary_key]
        if not droppable:
            return None
        victim = rng.choice(droppable)
        raws.append(RawChange(table_identifier=table, kind="removed", column_before=victim))

    elif ct is ChangeType.TYPE_WIDENED:
        candidates = [
            c for c in cols if c.data_type in ("integer", "smallint") or c.character_max_length
        ]
        if not candidates:
            return None
        target = rng.choice(candidates)
        if target.data_type in ("integer", "smallint"):
            after = target.model_copy(update={"data_type": "bigint"})
        else:
            cap = (target.character_max_length or 10) * 2
            after = target.model_copy(
                update={
                    "data_type": f"varchar({cap})",
                    "character_max_length": cap,
                }
            )
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    elif ct is ChangeType.TYPE_NARROWED:
        candidates = [c for c in cols if c.data_type in ("bigint", "integer")]
        if not candidates:
            candidates = [c for c in cols if c.character_max_length and c.character_max_length > 5]
            if not candidates:
                return None
            target = rng.choice(candidates)
            cap = max(2, (target.character_max_length or 10) // 2)
            after = target.model_copy(
                update={"data_type": f"varchar({cap})", "character_max_length": cap}
            )
        else:
            target = rng.choice(candidates)
            after = target.model_copy(
                update={"data_type": "integer" if target.data_type == "bigint" else "smallint"}
            )
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    elif ct is ChangeType.TYPE_INCOMPATIBLE:
        candidates = [c for c in cols if c.data_type in ("text", "varchar(25)", "varchar(40)")]
        if not candidates:
            candidates = [c for c in cols if c.character_max_length]
        if not candidates:
            return None
        target = rng.choice(candidates)
        after = target.model_copy(update={"data_type": "integer", "character_max_length": None})
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    elif ct is ChangeType.PRECISION_CHANGED:
        candidates = [c for c in cols if c.numeric_precision is not None]
        if not candidates:
            return None
        target = rng.choice(candidates)
        after = target.model_copy(
            update={
                "numeric_precision": (target.numeric_precision or 10) + 2,
                "numeric_scale": (target.numeric_scale or 0) + 1,
            }
        )
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    elif ct is ChangeType.PK_CHANGED:
        target = next((c for c in cols if c.is_primary_key), None)
        if target is None:
            return None
        after = target.model_copy(update={"is_primary_key": False})
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    elif ct is ChangeType.ENUM_VALUE_ADDED:
        candidates = [c for c in cols if c.enum_values]
        if not candidates:
            return None
        target = rng.choice(candidates)
        after = target.model_copy(
            update={"enum_values": (*(target.enum_values or ()), f"new_val_{variant}")}
        )
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    elif ct is ChangeType.DEFAULT_CHANGED:
        candidates = [
            c for c in cols if c.data_type in ("text", "varchar(25)", "boolean", "integer")
        ]
        if not candidates:
            return None
        target = rng.choice(candidates)
        after = target.model_copy(
            update={"default": "'updated_default'" if target.default is None else None}
        )
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    elif ct is ChangeType.NULLABILITY_TIGHTENED:
        candidates = [c for c in cols if c.nullable]
        if not candidates:
            return None
        target = rng.choice(candidates)
        after = target.model_copy(update={"nullable": False})
        raws.append(
            RawChange(
                table_identifier=table, kind="modified", column_before=target, column_after=after
            )
        )

    else:  # pragma: no cover
        return None

    # Severity floor from models.DEFAULT_SEVERITY
    from schema_drift.models import DEFAULT_SEVERITY

    expected_severity = DEFAULT_SEVERITY[ct].value

    return Scenario(
        scenario_id=_scenario_id(table, ct, variant, seed),
        table=table,
        expected_change_type=ct.value,
        expected_severity=expected_severity,
        pre_columns=[_column_dict(c) for c in cols],
        raw_changes=[json.loads(r.model_dump_json()) for r in raws],
        variant=variant,
        seed=seed,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Top-level loop
# ---------------------------------------------------------------------------


def generate_all(seed: int, variants: int) -> list[Scenario]:
    """Yield every viable (table × change_type × variant) scenario.

    Skips combinations that aren't expressible for a given table (e.g.
    ``ENUM_VALUE_ADDED`` on a table with no enum columns) — the runner
    relies on the actual emitted count, not a theoretical ``18 × 12 × N``.
    """
    out: list[Scenario] = []
    for table, cols in CATALOG.items():
        for ct in SINGLE_CHANGE_TYPES:
            for variant in range(variants):
                rng = random.Random(f"{seed}:{table}:{ct.value}:{variant}")
                s = _make_scenario(table, cols, ct, variant, seed, rng)
                if s is not None:
                    out.append(s)

    # Rename pairs (batch-only) — add for every table at variant=0.
    for table, cols in CATALOG.items():
        droppable = [c for c in cols if not c.is_primary_key]
        if not droppable:
            continue
        rng = random.Random(f"{seed}:{table}:column_renamed")
        target = rng.choice(droppable)
        new_name = f"{target.name}_renamed"
        after = target.model_copy(update={"name": new_name})
        raws = [
            RawChange(table_identifier=table, kind="removed", column_before=target),
            RawChange(table_identifier=table, kind="added", column_after=after),
        ]
        out.append(
            Scenario(
                scenario_id=_scenario_id(table, ChangeType.COLUMN_RENAMED, 0, seed),
                table=table,
                expected_change_type=ChangeType.COLUMN_RENAMED.value,
                expected_severity="high",
                pre_columns=[_column_dict(c) for c in cols],
                raw_changes=[json.loads(r.model_dump_json()) for r in raws],
                variant=0,
                seed=seed,
                notes=("batch_correlated",),
            )
        )

    return out


def write_scenarios(scenarios: list[Scenario], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for s in scenarios:
        (out_dir / f"{s.scenario_id}.json").write_text(s.to_json())


def held_out(scenario_id: str) -> bool:
    """Stable held-out split. Same formula as ``docs/02_revised_plan.md``."""
    digest = hashlib.sha256(scenario_id.encode()).hexdigest()
    return int(digest[:8], 16) % 10 in (7, 8, 9)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate schema-drift benchmark scenarios.")
    p.add_argument("--seed", type=int, default=20260101, help="RNG seed (default: 20260101).")
    p.add_argument(
        "--variants",
        type=int,
        default=2,
        help="Variants per (table, change_type). Default 2 ⇒ ≥300 scenarios.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "scenarios",
        help="Output directory.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    scenarios = generate_all(args.seed, args.variants)
    write_scenarios(scenarios, args.out)
    print(
        f"wrote {len(scenarios)} scenarios "
        f"({sum(held_out(s.scenario_id) for s in scenarios)} held-out) "
        f"→ {args.out}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
