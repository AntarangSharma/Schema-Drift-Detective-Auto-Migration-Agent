"""dbt-tests baseline.

The honest comparison
---------------------
dbt schema tests run *after* a model fails. They catch drift only when
the change makes a downstream test (not_null, unique, accepted_values,
relationships) blow up. That means:

* Drop a column referenced downstream → ``column not found`` ⇒ detected.
* Type narrow / incompatible → may run silently to completion ⇒ missed.
* Add a nullable column → no downstream effect ⇒ missed.

We encode that policy in the stub below, then in Week 6+ a real
``dbt test`` invocation will replace the rules with ground-truth pass/
fail counts on a held-out project.

Why this baseline matters
-------------------------
It's the *do-nothing* baseline for shops that already use dbt. If our
classifier doesn't outperform plain ``dbt test``, we have no story.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from schema_drift.models import RawChange


@dataclass(slots=True)
class DbtTestsBaseline:
    name: str = "dbt"

    def predict(self, raws: Sequence[RawChange]) -> tuple[str | None, str | None]:
        if not raws:
            return None, None
        for r in raws:
            # Drops always break downstream models → detected.
            if r.kind == "removed":
                return "unknown", "high"
            # Modifications might break depending on what changed.
            if r.kind == "modified" and r.column_before and r.column_after:
                b, a = r.column_before, r.column_after
                # Nullability tightening breaks not_null on the downstream
                # ``staging`` model that previously allowed NULLs.
                if b.nullable and not a.nullable:
                    return "unknown", "medium"
                # Type incompatibility breaks the staging cast.
                if b.data_type.split("(")[0] != a.data_type.split("(")[0]:
                    return "unknown", "high"
        return None, None
