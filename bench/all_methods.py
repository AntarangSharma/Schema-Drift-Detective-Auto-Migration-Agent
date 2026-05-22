"""Run every method against the held-out split + write RESULTS.md.

This is the script invoked by ``make bench`` at the end of Week 5.
It glues the runner, every baseline, the confusion renderer, and the
markdown updater into a single command so the README's benchmark
section can never silently drift from the actual scores.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bench.confusion import render as render_confusion
from bench.runner import _print_summary, run

METHODS: tuple[str, ...] = ("ge", "dbt", "oneshot", "ours")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Score every method, refresh RESULTS.md.")
    p.add_argument(
        "--scenarios",
        type=Path,
        default=Path(__file__).parent / "scenarios",
    )
    p.add_argument(
        "--results",
        type=Path,
        default=Path(__file__).parent / "results",
    )
    p.add_argument(
        "--held-out-only",
        action="store_true",
        help="Score against the held-out split only (the W5 headline number).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    summaries = []
    for method in METHODS:
        summary = run(method, args.scenarios, args.results, only_held_out=args.held_out_only)
        _print_summary(summary)
        summaries.append(summary)
        # Render and write a per-method text confusion matrix next to results.
        matrix_path = args.results / f"confusion-{method}.txt"
        matrix_path.write_text(render_confusion(summary.confusion))
    print(f"\n[bench] wrote {len(summaries)} method results + matrices to {args.results}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
