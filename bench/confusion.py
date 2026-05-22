"""Text-only confusion-matrix renderer.

CI must stay matplotlib-free (and image-free, for that matter), so we
render confusion matrices as ASCII tables. The function takes the same
``{expected: {predicted: count}}`` dict the runner already emits.

The renderer:

* sorts rows and columns by frequency (most-common first) for legibility,
* truncates long ChangeType names to the first 22 chars,
* pads counts so columns align,
* prints a header summarising row/column totals.

We keep the renderer separate from ``runner.py`` because the runner is
on the hot path of every bench invocation; this module is only loaded
when someone asks for a matrix.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping


def render(confusion: Mapping[str, Mapping[str, int]], *, max_width: int = 22) -> str:
    """Render a confusion matrix as a plain-text table."""
    if not confusion:
        return "(no data)"

    # Collect every label that appears anywhere in the matrix.
    col_totals: Counter[str] = Counter()
    for predicted_map in confusion.values():
        for pred, count in predicted_map.items():
            col_totals[pred] += count
    row_totals = {row: sum(predicted_map.values()) for row, predicted_map in confusion.items()}

    # Order rows/cols by total descending (so the most common labels are
    # visible without scrolling).
    rows = sorted(confusion.keys(), key=lambda r: row_totals[r], reverse=True)
    cols = [c for c, _ in col_totals.most_common()]

    def _trunc(s: str) -> str:
        return s if len(s) <= max_width else s[: max_width - 1] + "…"

    # Compute column widths: max of header text or any cell digit-count.
    col_widths = []
    for c in cols:
        w = max(len(_trunc(c)), 3)
        for r in rows:
            v = confusion.get(r, {}).get(c, 0)
            w = max(w, len(str(v)))
        col_widths.append(w)

    # Row label width.
    row_label_w = max((len(_trunc(r)) for r in rows), default=0)
    row_label_w = max(row_label_w, len("expected ↓ / pred →"))

    lines: list[str] = []
    # Header.
    header_cells = [
        "expected ↓ / pred →".ljust(row_label_w),
        *(_trunc(c).rjust(w) for c, w in zip(cols, col_widths, strict=False)),
        "Σ".rjust(5),
    ]
    lines.append(" | ".join(header_cells))
    lines.append("-+-".join(["-" * len(part) for part in header_cells]))

    for r in rows:
        cells = [_trunc(r).ljust(row_label_w)]
        for c, w in zip(cols, col_widths, strict=False):
            v = confusion.get(r, {}).get(c, 0)
            cells.append(str(v).rjust(w))
        cells.append(str(row_totals[r]).rjust(5))
        lines.append(" | ".join(cells))

    # Column totals row.
    cells = ["Σ".ljust(row_label_w)]
    grand_total = 0
    for c, w in zip(cols, col_widths, strict=False):
        cells.append(str(col_totals[c]).rjust(w))
        grand_total += col_totals[c]
    cells.append(str(grand_total).rjust(5))
    lines.append(" | ".join(cells))

    return "\n".join(lines)


__all__ = ["render"]
