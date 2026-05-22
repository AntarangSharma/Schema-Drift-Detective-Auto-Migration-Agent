"""Tests for the policy engine.

The engine is pure (apart from the rate-limit clock + the env-var
kill switch), so we test it as a state machine: feed it a sequence
of events, assert the produced ``PolicyDecision`` chain.
"""

from __future__ import annotations

import pytest

from schema_drift.models import (
    DEFAULT_SEVERITY,
    Action,
    ChangeType,
    DriftEvent,
    ImpactSet,
    Severity,
    SourceKind,
)
from schema_drift.policy import KILL_SWITCH_ENV, PolicyEngine


def _event(
    change_type: ChangeType,
    *,
    severity: Severity | None = None,
    impact: ImpactSet | None = None,
) -> DriftEvent:
    return DriftEvent(
        source_system=SourceKind.POSTGRES,
        source_identifier="source_raw.orders.customer_id",
        change_type=change_type,
        severity=severity or DEFAULT_SEVERITY[change_type],
        impact=impact or ImpactSet(),
    )


def _impact(models: tuple[str, ...] = (), blast: float = 0.0, fan_out: bool = False) -> ImpactSet:
    return ImpactSet(
        dbt_models=models,
        blast_radius_score=blast,
        fan_out_conservative=fan_out,
    )


class TestKillSwitch:
    def test_kill_switch_ignores_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KILL_SWITCH_ENV, "1")
        eng = PolicyEngine()
        d = eng.decide(_event(ChangeType.COLUMN_DROPPED))
        assert d.action is Action.IGNORE
        assert "kill switch" in d.reason
        assert "kill-switch" in d.labels

    def test_kill_switch_off_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)
        eng = PolicyEngine()
        d = eng.decide(_event(ChangeType.COLUMN_ADDED_NULLABLE, impact=_impact(("m",))))
        assert d.action is Action.OPEN_PR


class TestDestructiveGate:
    @pytest.mark.parametrize(
        "ct",
        [
            ChangeType.COLUMN_DROPPED,
            ChangeType.TYPE_NARROWED,
            ChangeType.TYPE_INCOMPATIBLE,
            ChangeType.PK_CHANGED,
            ChangeType.PARTITION_KEY_CHANGED,
        ],
    )
    def test_destructive_change_forces_draft_pr(self, ct: ChangeType) -> None:
        eng = PolicyEngine()
        d = eng.decide(_event(ct))
        assert d.action is Action.OPEN_DRAFT_PR
        assert "requires-human" in d.labels
        assert "destructive" in d.labels


class TestSeverityFlow:
    def test_high_severity_always_drafts(self) -> None:
        eng = PolicyEngine()
        ev = _event(ChangeType.COLUMN_RENAMED)  # HIGH but destructive=False
        # COLUMN_RENAMED is NOT in DESTRUCTIVE_CHANGES, so we hit the
        # severity branch, not the destructive branch.
        d = eng.decide(ev)
        assert d.action is Action.OPEN_DRAFT_PR
        assert "requires-human" in d.labels

    def test_medium_severity_opens_pr(self) -> None:
        eng = PolicyEngine()
        d = eng.decide(_event(ChangeType.PRECISION_CHANGED, impact=_impact(("m",))))
        assert d.action is Action.OPEN_PR

    def test_low_severity_with_impact_opens_pr(self) -> None:
        eng = PolicyEngine()
        d = eng.decide(_event(ChangeType.COLUMN_ADDED_NULLABLE, impact=_impact(("stg_orders",))))
        assert d.action is Action.OPEN_PR

    def test_low_severity_no_impact_is_alert_only(self) -> None:
        eng = PolicyEngine()
        d = eng.decide(_event(ChangeType.COLUMN_ADDED_NULLABLE))
        assert d.action is Action.ALERT_ONLY


class TestBlastRadiusCap:
    def test_medium_above_cap_downgrades_to_draft(self) -> None:
        eng = PolicyEngine(blast_radius_cap=10.0)
        ev = _event(
            ChangeType.PRECISION_CHANGED,
            impact=_impact(models=("a", "b", "c"), blast=99.0),
        )
        d = eng.decide(ev)
        assert d.action is Action.OPEN_DRAFT_PR
        assert "large-blast" in d.labels

    def test_low_above_cap_drafts_with_label(self) -> None:
        eng = PolicyEngine(blast_radius_cap=5.0)
        ev = _event(
            ChangeType.COLUMN_ADDED_NULLABLE,
            impact=_impact(models=("m",), blast=50.0),
        )
        d = eng.decide(ev)
        assert d.action is Action.OPEN_DRAFT_PR
        assert "large-blast" in d.labels


class TestRateLimit:
    def test_rate_limit_kicks_in_after_n_events(self) -> None:
        eng = PolicyEngine(max_events_per_window=3, window_seconds=3600)
        ev = _event(ChangeType.PRECISION_CHANGED, impact=_impact(("m",)))
        for _ in range(3):
            assert eng.decide(ev).action is Action.OPEN_PR
        d = eng.decide(ev)
        assert d.action is Action.ALERT_ONLY
        assert "rate-limited" in d.labels

    def test_alert_only_does_not_count_toward_rate_limit(self) -> None:
        eng = PolicyEngine(max_events_per_window=2)
        # LOW + no impact is alert-only and should NOT consume budget.
        for _ in range(5):
            d = eng.decide(_event(ChangeType.COLUMN_ADDED_NULLABLE))
            assert d.action is Action.ALERT_ONLY
        # Budget is still 2: two PRs allowed.
        for _ in range(2):
            assert (
                eng.decide(_event(ChangeType.PRECISION_CHANGED, impact=_impact(("m",)))).action
                is Action.OPEN_PR
            )


class TestLabels:
    def test_fan_out_widened_label_propagates(self) -> None:
        eng = PolicyEngine()
        ev = _event(
            ChangeType.PRECISION_CHANGED,
            impact=_impact(models=("m",), fan_out=True),
        )
        d = eng.decide(ev)
        assert "fan-out-widened" in d.labels
        assert any(label.startswith("drift:") for label in d.labels)
        assert any(label.startswith("severity:") for label in d.labels)
