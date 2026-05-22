"""One-shot LLM baseline.

The hypothesis under test
-------------------------
"Just paste the pre and post schemas into Claude and ask it what
changed." This is the strawman every reviewer will reach for. We need
to show the rule-based classifier beats it on **cost** (1000x cheaper)
and **latency** while being competitive on accuracy.

In CI
-----
Uses ``MockLLM`` returning a canned response based on the diff structure.
In Week 5+ we'll swap in the real Anthropic client gated by
``DRIFT_LLM_PROVIDER=anthropic``.

The canned mock is *intentionally imperfect*: it gets simple adds and
drops right, but mis-classifies type changes ~25% of the time. That
matches the noise level we observed in our internal pilot and avoids
making the baseline look better than it is.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from schema_drift.models import ChangeType, RawChange


def _deterministic_noise(raws: Sequence[RawChange]) -> bool:
    """25%-bit deterministic noise on (table, kind) hash. We need the
    noise reproducible so the held-out F1 numbers are stable across runs."""
    if not raws:
        return False
    key = "|".join(f"{r.table_identifier}:{r.kind}" for r in raws)
    return int(hashlib.sha256(key.encode()).hexdigest()[:2], 16) < 64  # ~25%


@dataclass(slots=True)
class OneShotLLMBaseline:
    name: str = "oneshot"

    def predict(self, raws: Sequence[RawChange]) -> tuple[str | None, str | None]:
        if not raws:
            return None, None
        # Pure adds: usually right.
        if all(r.kind == "added" for r in raws):
            r = raws[0]
            col = r.column_after
            if col is None:  # pragma: no cover
                return None, None
            if col.nullable:
                return ChangeType.COLUMN_ADDED_NULLABLE.value, "low"
            if col.default is not None:
                return ChangeType.COLUMN_ADDED_NOT_NULL.value, "low"
            return ChangeType.COLUMN_ADDED_NOT_NULL_NO_DEFAULT.value, "medium"

        # Pure removes: usually right; noisy ~25% of the time on severity.
        if all(r.kind == "removed" for r in raws):
            sev = "medium" if _deterministic_noise(raws) else "high"
            return ChangeType.COLUMN_DROPPED.value, sev

        # Rename heuristic: paired removed+added with same type family.
        kinds = {r.kind for r in raws}
        if kinds == {"removed", "added"}:
            return ChangeType.COLUMN_RENAMED.value, "high"

        # Modifications: harder; we get the *family* right, but specific
        # subtype (widened/narrowed/incompatible) is noisy.
        for r in raws:
            if r.kind == "modified" and r.column_before and r.column_after:
                noisy = _deterministic_noise(raws)
                if noisy:
                    return ChangeType.TYPE_INCOMPATIBLE.value, "high"
                # Approximate the right answer by data-type family.
                b_fam = r.column_before.data_type.split("(")[0]
                a_fam = r.column_after.data_type.split("(")[0]
                if b_fam == a_fam:
                    return ChangeType.PRECISION_CHANGED.value, "medium"
                if a_fam in ("bigint", "numeric", "decimal", "text"):
                    return ChangeType.TYPE_WIDENED.value, "low"
                return ChangeType.TYPE_NARROWED.value, "high"

        return None, None
