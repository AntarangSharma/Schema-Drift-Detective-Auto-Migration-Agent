"""Tests for audit.py, ol.py, and slack.py.

These three modules all share the same contract: *no-op when their
env var is unset*. We test that contract first, then the happy-path
payload shapes via mock httpx transports.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from schema_drift.audit import (
    AUDIT_PATH_ENV,
    Auditor,
    InMemorySink,
    JsonlFileSink,
    StdoutSink,
)
from schema_drift.models import (
    DEFAULT_SEVERITY,
    Action,
    ChangeType,
    DriftEvent,
    ImpactSet,
    Severity,
    SourceKind,
)
from schema_drift.ol import (
    OL_PARENT_FACET_SCHEMA_URL,
    OL_PRODUCER_URI,
    OL_RUN_EVENT_SCHEMA_URL,
    OLConfig,
    OpenLineageEmitter,
    ParentRunFacet,
)
from schema_drift.slack import SLACK_WEBHOOK_ENV, SlackConfig, SlackNotifier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(ct: ChangeType = ChangeType.COLUMN_DROPPED, **kw) -> DriftEvent:
    base = {
        "source_system": SourceKind.POSTGRES,
        "source_identifier": "source_raw.orders.customer_id",
        "change_type": ct,
        "severity": DEFAULT_SEVERITY[ct],
    }
    base.update(kw)
    return DriftEvent(**base)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAuditor:
    def test_in_memory_sink_captures_records(self) -> None:
        sink = InMemorySink()
        a = Auditor(sink=sink)
        rec = a.emit("Classifier", "classified", target="evt-1", payload={"n": 3})
        assert sink.records == [rec]
        assert rec.actor == "Classifier"
        assert rec.action == "classified"
        assert rec.payload == {"n": 3}

    def test_jsonl_sink_appends(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        sink = JsonlFileSink(path)
        a = Auditor(sink=sink)
        a.emit("A", "x")
        a.emit("B", "y", target="t", payload={"k": "v"})
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        decoded = [json.loads(line) for line in lines]
        assert decoded[0]["actor"] == "A"
        assert decoded[1]["payload"] == {"k": "v"}
        assert decoded[1]["target"] == "t"

    def test_default_sink_uses_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "trail.jsonl"
        monkeypatch.setenv(AUDIT_PATH_ENV, str(path))
        a = Auditor()
        assert isinstance(a.sink, JsonlFileSink)
        a.emit("x", "y")
        assert path.exists()

    def test_default_sink_without_env_is_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(AUDIT_PATH_ENV, raising=False)
        a = Auditor()
        assert isinstance(a.sink, StdoutSink)


# ---------------------------------------------------------------------------
# OpenLineage
# ---------------------------------------------------------------------------


class _RecordingTransport(httpx.MockTransport):
    """Captures the last request the emitter sent."""

    def __init__(self, status_code: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = status_code

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(status_code, json={"ok": True})

        super().__init__(handler)


class TestOpenLineage:
    def test_disabled_when_url_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
        em = OpenLineageEmitter()
        assert em.enabled is False
        assert em.emit(_event()) is False

    def test_post_payload_has_drift_facet(self) -> None:
        tr = _RecordingTransport()
        client = httpx.Client(transport=tr)
        em = OpenLineageEmitter(OLConfig(url="http://marquez:5000"), client=client)
        ev = _event(
            impact=ImpactSet(dbt_models=("stg_orders", "fct_orders"), blast_radius_score=4.2)
        )
        assert em.emit(ev) is True

        assert len(tr.requests) == 1
        body = json.loads(tr.requests[0].read())
        assert body["eventType"] == "COMPLETE"
        assert body["run"]["facets"]["drift"]["change_type"] == ChangeType.COLUMN_DROPPED.value
        assert body["run"]["facets"]["drift"]["severity"] == Severity.HIGH.value
        assert body["run"]["facets"]["drift"]["blast_radius_score"] == 4.2
        assert {o["name"] for o in body["outputs"]} == {"stg_orders", "fct_orders"}
        assert body["inputs"][0]["name"] == "source_raw.orders.customer_id"
        assert body["job"]["name"] == "schema_drift_detect"

    def test_swallows_http_error(self) -> None:
        tr = _RecordingTransport(status_code=500)
        client = httpx.Client(transport=tr)
        em = OpenLineageEmitter(OLConfig(url="http://marquez:5000"), client=client)
        # 500 → raise_for_status → caught → returns False
        assert em.emit(_event()) is False

    def test_parent_run_facet_emitted_when_configured(self) -> None:
        """When OPENLINEAGE_PARENT_RUN_ID is configured, the emitted
        RunEvent must carry a spec-compliant ParentRunFacet under
        ``run.facets.parent`` with the three required sub-fields and
        the two mandatory facet metadata fields.
        """
        tr = _RecordingTransport()
        client = httpx.Client(transport=tr)
        parent = ParentRunFacet(
            run_id="01HBX1234ABCDEF",
            job_namespace="dbt-prod",
            job_name="dbt.run.daily_refresh",
        )
        em = OpenLineageEmitter(OLConfig(url="http://marquez:5000", parent=parent), client=client)
        assert em.emit(_event()) is True

        body = json.loads(tr.requests[0].read())
        assert body["schemaURL"] == OL_RUN_EVENT_SCHEMA_URL
        assert body["producer"] == OL_PRODUCER_URI
        parent_facet = body["run"]["facets"]["parent"]
        # Spec-mandated facet metadata
        assert parent_facet["_producer"] == OL_PRODUCER_URI
        assert parent_facet["_schemaURL"] == OL_PARENT_FACET_SCHEMA_URL
        # Spec-mandated payload fields
        assert parent_facet["run"]["runId"] == "01HBX1234ABCDEF"
        assert parent_facet["job"]["namespace"] == "dbt-prod"
        assert parent_facet["job"]["name"] == "dbt.run.daily_refresh"
        # Drift facet is still present
        assert "drift" in body["run"]["facets"]

    def test_no_parent_facet_when_unconfigured(self) -> None:
        tr = _RecordingTransport()
        client = httpx.Client(transport=tr)
        em = OpenLineageEmitter(OLConfig(url="http://marquez:5000"), client=client)
        em.emit(_event())
        body = json.loads(tr.requests[0].read())
        assert "parent" not in body["run"]["facets"]

    def test_parent_from_env_requires_all_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # All three set → builds.
        monkeypatch.setenv("OPENLINEAGE_PARENT_RUN_ID", "01HBXFOO")
        monkeypatch.setenv("OPENLINEAGE_PARENT_JOB_NAME", "dbt.run")
        monkeypatch.setenv("OPENLINEAGE_PARENT_JOB_NAMESPACE", "prod")
        p = ParentRunFacet.from_env()
        assert p is not None
        assert p.job_namespace == "prod"

        # Missing run_id → None (no half-configured parents).
        monkeypatch.delenv("OPENLINEAGE_PARENT_RUN_ID")
        assert ParentRunFacet.from_env() is None

    def test_constructor_parent_overrides_config_parent(self) -> None:
        """ctor-level parent must win over config-level — used by the
        CLI to plug a freshly-minted parent in without touching env."""
        tr = _RecordingTransport()
        client = httpx.Client(transport=tr)
        config_parent = ParentRunFacet("OLD", "ns", "old.job")
        ctor_parent = ParentRunFacet("NEW", "ns", "new.job")
        em = OpenLineageEmitter(
            OLConfig(url="http://marquez:5000", parent=config_parent),
            client=client,
            parent=ctor_parent,
        )
        em.emit(_event())
        body = json.loads(tr.requests[0].read())
        assert body["run"]["facets"]["parent"]["run"]["runId"] == "NEW"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class TestSlack:
    def test_disabled_when_webhook_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SLACK_WEBHOOK_ENV, raising=False)
        n = SlackNotifier()
        assert n.enabled is False
        assert n.notify(_event(), Action.OPEN_PR) is False

    def test_payload_includes_severity_emoji_and_action(self) -> None:
        n = SlackNotifier(SlackConfig(webhook_url="http://slack/hook"))
        ev = _event(impact=ImpactSet(dbt_models=("a", "b", "c"), blast_radius_score=12.0))
        payload = n.build_payload(
            ev, Action.OPEN_DRAFT_PR, pr_url="https://github.com/o/r/pull/7", reason="HIGH"
        )
        assert ":red_circle:" in payload["text"]
        # Header + section + context + actions = 4 blocks.
        kinds = [b["type"] for b in payload["blocks"]]
        assert kinds == ["header", "section", "context", "actions"]
        sec_fields = payload["blocks"][1]["fields"]
        action_field = next(f for f in sec_fields if "*Action*" in f["text"])
        assert "drafted PR" in action_field["text"]
        models_field = next(f for f in sec_fields if "*Affected models*" in f["text"])
        assert "a, b, c" in models_field["text"]
        blast_field = next(f for f in sec_fields if "*Blast radius*" in f["text"])
        assert "12.0" in blast_field["text"]
        # Action button preserves the PR URL.
        btn = payload["blocks"][3]["elements"][0]
        assert btn["url"] == "https://github.com/o/r/pull/7"

    def test_post_lands_on_webhook(self) -> None:
        tr = _RecordingTransport()
        client = httpx.Client(transport=tr)
        n = SlackNotifier(SlackConfig(webhook_url="http://slack/hook"), client=client)
        assert n.notify(_event(), Action.ALERT_ONLY, reason="just FYI") is True
        assert len(tr.requests) == 1
        body = json.loads(tr.requests[0].read())
        assert "blocks" in body

    def test_payload_truncates_long_model_list(self) -> None:
        n = SlackNotifier(SlackConfig(webhook_url="http://slack/hook"))
        models = tuple(f"m{i}" for i in range(20))
        ev = _event(impact=ImpactSet(dbt_models=models))
        payload = n.build_payload(ev, Action.OPEN_PR)
        models_field = next(
            f for f in payload["blocks"][1]["fields"] if "*Affected models*" in f["text"]
        )
        assert "+15 more" in models_field["text"]
