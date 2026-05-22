"""Tests for the Prometheus metrics module.

We bind metrics to a fresh ``CollectorRegistry`` per test to avoid
cross-test state. The singleton path is exercised once at the bottom.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from schema_drift.metrics import DriftMetrics, metrics


def _build() -> DriftMetrics:
    return DriftMetrics.build(CollectorRegistry())


class TestCounters:
    def test_event_counter_labels(self) -> None:
        m = _build()
        m.record_event("column_dropped", "high")
        m.record_event("column_dropped", "high")
        m.record_event("column_added_nullable", "low")
        text = m.render().decode()
        assert 'schema_drift_events_total{change_type="column_dropped",severity="high"} 2.0' in text
        assert (
            'schema_drift_events_total{change_type="column_added_nullable",severity="low"} 1.0'
            in text
        )

    def test_action_counter(self) -> None:
        m = _build()
        m.record_action("open_pr", "medium")
        text = m.render().decode()
        assert 'schema_drift_actions_total{action="open_pr",severity="medium"} 1.0' in text

    def test_llm_records_tokens_in_and_out(self) -> None:
        m = _build()
        m.record_llm(
            provider="anthropic",
            model="claude-3-5-sonnet",
            tokens_in=1000,
            tokens_out=200,
            cost_usd=0.012,
        )
        text = m.render().decode()
        assert "schema_drift_llm_tokens_total" in text
        assert 'direction="in"' in text
        assert 'direction="out"' in text
        # Histogram observation lands in a bucket.
        assert "schema_drift_llm_cost_usd_bucket" in text

    def test_pr_opened_records_draft_state(self) -> None:
        m = _build()
        m.record_pr_opened(is_draft=True)
        m.record_pr_opened(is_draft=False)
        text = m.render().decode()
        assert 'schema_drift_prs_opened_total{is_draft="true"} 1.0' in text
        assert 'schema_drift_prs_opened_total{is_draft="false"} 1.0' in text

    def test_pipeline_error_counter(self) -> None:
        m = _build()
        m.record_error("classifier")
        m.record_error("classifier")
        m.record_error("pr_gateway")
        text = m.render().decode()
        assert 'schema_drift_pipeline_errors_total{component="classifier"} 2.0' in text
        assert 'schema_drift_pipeline_errors_total{component="pr_gateway"} 1.0' in text


class TestHistogram:
    def test_classifier_latency_observation(self) -> None:
        m = _build()
        m.classifier_latency_seconds.observe(0.003)
        m.classifier_latency_seconds.observe(0.04)
        text = m.render().decode()
        assert "schema_drift_classifier_latency_seconds_count 2.0" in text


class TestSingleton:
    def test_singleton_is_stable_across_calls(self) -> None:
        DriftMetrics.reset_singleton()
        a = metrics()
        b = metrics()
        assert a is b
        DriftMetrics.reset_singleton()
        c = metrics()
        assert c is not a
