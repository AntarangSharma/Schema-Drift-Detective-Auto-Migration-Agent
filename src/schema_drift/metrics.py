"""Prometheus metrics.

Exposes a small, deliberately-named registry of counters and histograms
so an operator can build a single Grafana row from one PromQL block.

Naming convention
-----------------
Every metric starts with ``schema_drift_`` so a multi-tenant prom server
can grep them apart from neighbouring exporters. Counters end in
``_total``; histograms end in ``_seconds`` (latency) or ``_usd`` (cost).

Why a hand-rolled registry and not the default ``REGISTRY``
-----------------------------------------------------------
Tests need to reset counters between runs, and the default global
registry leaks state across the test suite (pytest re-imports the
module). We bind our metrics to a dedicated ``CollectorRegistry`` so
tests can construct a fresh one. Production code uses the default
``metrics()`` factory which lazily builds the singleton.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import ClassVar

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


@dataclass(slots=True)
class DriftMetrics:
    """Bound metric handles for one registry."""

    registry: CollectorRegistry

    events_total: Counter
    actions_total: Counter
    classifier_latency_seconds: Histogram
    llm_cost_usd: Histogram
    llm_tokens_total: Counter
    prs_opened_total: Counter
    pipeline_errors_total: Counter

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    _singleton: ClassVar[DriftMetrics | None] = None
    _lock: ClassVar[Lock] = Lock()

    @classmethod
    def get(cls) -> DriftMetrics:
        """Return the process-wide singleton (lazy)."""
        with cls._lock:
            if cls._singleton is None:
                cls._singleton = cls.build(CollectorRegistry())
            return cls._singleton

    @classmethod
    def reset_singleton(cls) -> None:
        """Tests call this; production code never should."""
        with cls._lock:
            cls._singleton = None

    @classmethod
    def build(cls, registry: CollectorRegistry) -> DriftMetrics:
        return cls(
            registry=registry,
            events_total=Counter(
                "schema_drift_events_total",
                "Drift events classified, by change_type and severity.",
                labelnames=("change_type", "severity"),
                registry=registry,
            ),
            actions_total=Counter(
                "schema_drift_actions_total",
                "Policy actions taken, by action and severity.",
                labelnames=("action", "severity"),
                registry=registry,
            ),
            classifier_latency_seconds=Histogram(
                "schema_drift_classifier_latency_seconds",
                "Classifier latency per batch.",
                buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
                registry=registry,
            ),
            llm_cost_usd=Histogram(
                "schema_drift_llm_cost_usd",
                "LLM cost in USD per migration draft.",
                buckets=(0.001, 0.01, 0.1, 0.5, 1.0, 5.0, 10.0),
                labelnames=("provider", "model"),
                registry=registry,
            ),
            llm_tokens_total=Counter(
                "schema_drift_llm_tokens_total",
                "LLM tokens consumed.",
                labelnames=("direction", "provider", "model"),  # in|out
                registry=registry,
            ),
            prs_opened_total=Counter(
                "schema_drift_prs_opened_total",
                "PRs opened (and whether they're drafts).",
                labelnames=("is_draft",),
                registry=registry,
            ),
            pipeline_errors_total=Counter(
                "schema_drift_pipeline_errors_total",
                "Unhandled pipeline errors by component.",
                labelnames=("component",),
                registry=registry,
            ),
        )

    # ------------------------------------------------------------------ #
    # Convenience emitters                                                #
    # ------------------------------------------------------------------ #

    def record_event(self, change_type: str, severity: str) -> None:
        self.events_total.labels(change_type=change_type, severity=severity).inc()

    def record_action(self, action: str, severity: str) -> None:
        self.actions_total.labels(action=action, severity=severity).inc()

    def record_llm(
        self,
        *,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
    ) -> None:
        self.llm_cost_usd.labels(provider=provider, model=model).observe(cost_usd)
        self.llm_tokens_total.labels(direction="in", provider=provider, model=model).inc(tokens_in)
        self.llm_tokens_total.labels(direction="out", provider=provider, model=model).inc(
            tokens_out
        )

    def record_pr_opened(self, *, is_draft: bool) -> None:
        self.prs_opened_total.labels(is_draft=str(is_draft).lower()).inc()

    def record_error(self, component: str) -> None:
        self.pipeline_errors_total.labels(component=component).inc()

    # ------------------------------------------------------------------ #
    # Scrape format                                                       #
    # ------------------------------------------------------------------ #

    def render(self) -> bytes:
        """Return the Prometheus text-exposition payload."""
        return generate_latest(self.registry)


def metrics() -> DriftMetrics:
    """Get the process singleton. Convenience wrapper."""
    return DriftMetrics.get()


__all__ = ["DriftMetrics", "metrics"]
