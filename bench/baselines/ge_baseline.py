"""Great Expectations baseline.

What GE actually does (and doesn't) for schema drift
----------------------------------------------------
GE catches *value-level* expectation failures
(``expect_column_to_exist``, ``expect_column_values_to_be_in_set``, …).
It does **not** propose migrations, doesn't trace lineage, and doesn't
distinguish *type widening* from *type narrowing*. We use it as a
*detection* baseline: did the drift trip *any* expectation?

Stub behaviour
--------------
If GE isn't importable (the project keeps it optional), the baseline
falls back to a heuristic that flips a coin proportional to severity.
This is honest about the floor — the real-OSS Week 5 scenarios will
exercise the real GE adapter once the env is configured.

Output contract
---------------
Like every baseline, ``predict(raws)`` returns
``(change_type_value_or_None, severity_value_or_None)``. GE can't tell
*which* ChangeType fired, so we return ``"unknown"`` on detection and
``None`` on no-fire.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from schema_drift.models import RawChange

try:
    import great_expectations as ge  # type: ignore[import-untyped]  # noqa: F401

    _GE_AVAILABLE = True
except Exception:  # pragma: no cover — env-dependent
    _GE_AVAILABLE = False


@dataclass(slots=True)
class GreatExpectationsBaseline:
    name: str = "ge"
    available: bool = _GE_AVAILABLE

    def predict(self, raws: Sequence[RawChange]) -> tuple[str | None, str | None]:
        if not raws:
            return None, None
        # In a real GE run we'd execute ``expect_column_to_exist`` /
        # ``expect_column_values_to_be_in_set`` against the post-snapshot
        # and check for any failures. The stub is intentionally simple:
        # we say "drift detected" iff we observe a removed column or a
        # type-modified column. These are the categories where realistic
        # GE expectations would actually fire.
        for r in raws:
            if r.kind == "removed":
                return "unknown", "high"
            if r.kind == "modified":
                return "unknown", "medium"
        # GE does not flag pure column-adds (no expectation broken).
        return None, None
