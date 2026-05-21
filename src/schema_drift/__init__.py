"""Schema Drift Detective — an upstream schema-drift CI check.

Watches source schemas, classifies changes by breakage severity using
downstream lineage, and opens a GitHub PR with a proposed migration,
updated dbt tests, and a quantified impact report.

See docs/02_revised_plan.md for the design rationale.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("schema-drift-detective")
except PackageNotFoundError:  # pragma: no cover -- editable / source checkout
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
