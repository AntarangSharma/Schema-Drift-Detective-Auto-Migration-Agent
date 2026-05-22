"""Policy engine — turns a (DriftEvent, ImpactSet) pair into an Action.

The engine is intentionally small and deterministic. It is the only
component that decides whether a PR is opened, and it does so by
applying four gates in order:

1. **Kill switch** — ``DRIFT_KILL_SWITCH=1`` ⇒ everything becomes ``IGNORE``.
   Wired so an on-call can disarm the agent in seconds without a deploy.
2. **Rate limit** — at most ``max_events_per_window`` events per
   ``window_seconds`` produce a PR. Excess events are ``ALERT_ONLY``.
   Prevents an upstream schema explosion from spamming the repo.
3. **Destructive gate** — any ChangeType in ``DESTRUCTIVE_CHANGES`` is
   forced to ``OPEN_DRAFT_PR`` with a ``requires-human`` label (caller
   honours this via ``MigrationBundle.is_draft``). This is the rule
   the README promises: "destructive changes never auto-merge".
4. **Blast-radius cap** — if ``impact.blast_radius_score`` exceeds
   ``blast_radius_cap``, downgrade ``OPEN_PR`` to ``OPEN_DRAFT_PR``.
   Large blast radius ⇒ humans should look first.

Everything else flows through severity:
* HIGH ⇒ OPEN_DRAFT_PR (always require review)
* MEDIUM ⇒ OPEN_PR
* LOW ⇒ OPEN_PR if there *is* impact, else ALERT_ONLY (no zombie PRs
  for nullable adds against tables nothing touches)

The engine is pure; side effects (Slack ping, audit log) are the
caller's job.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field

from schema_drift.models import (
    DESTRUCTIVE_CHANGES,
    Action,
    DriftEvent,
    ImpactSet,
    Severity,
)

KILL_SWITCH_ENV = "DRIFT_KILL_SWITCH"


@dataclass(slots=True)
class PolicyDecision:
    """The engine's verdict + a human-readable explanation."""

    action: Action
    reason: str
    labels: tuple[str, ...] = ()


@dataclass(slots=True)
class PolicyEngine:
    """Stateful policy engine.

    State is just the rate-limit window. Re-instantiate freely; the
    only thing that survives a restart is the kill switch (env var).
    """

    blast_radius_cap: float = 25.0
    max_events_per_window: int = 10
    window_seconds: float = 3600.0
    _recent: deque[float] = field(default_factory=deque, init=False, repr=False)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def decide(self, event: DriftEvent, impact: ImpactSet | None = None) -> PolicyDecision:
        """Pure decision step. ``impact`` defaults to ``event.impact``."""
        impact = impact or event.impact

        if self._kill_switch_active():
            return PolicyDecision(
                action=Action.IGNORE,
                reason="kill switch active (DRIFT_KILL_SWITCH=1)",
                labels=("kill-switch",),
            )

        if not self._within_rate_limit():
            return PolicyDecision(
                action=Action.ALERT_ONLY,
                reason=(
                    f"rate limit exceeded "
                    f"(>{self.max_events_per_window}/{self.window_seconds:.0f}s)"
                ),
                labels=("rate-limited",),
            )

        # Record the event for the rate-limit window only if we're
        # going to produce a PR (alert-only doesn't count toward the cap).
        # We provisionally record, then roll back if we'd return ignore/alert.
        labels = self._base_labels(event, impact)

        if event.change_type in DESTRUCTIVE_CHANGES:
            self._record()
            return PolicyDecision(
                action=Action.OPEN_DRAFT_PR,
                reason=f"destructive change {event.change_type.value!r} requires human review",
                labels=(*labels, "requires-human", "destructive"),
            )

        # Blast radius cap downgrades OPEN_PR → OPEN_DRAFT_PR.
        large_blast = impact.blast_radius_score > self.blast_radius_cap

        match event.severity:
            case Severity.HIGH:
                self._record()
                return PolicyDecision(
                    action=Action.OPEN_DRAFT_PR,
                    reason="HIGH severity",
                    labels=(*labels, "requires-human"),
                )
            case Severity.MEDIUM:
                self._record()
                if large_blast:
                    return PolicyDecision(
                        action=Action.OPEN_DRAFT_PR,
                        reason=(
                            f"MEDIUM severity with blast_radius "
                            f"{impact.blast_radius_score:.1f} > cap {self.blast_radius_cap:.1f}"
                        ),
                        labels=(*labels, "requires-human", "large-blast"),
                    )
                return PolicyDecision(
                    action=Action.OPEN_PR,
                    reason="MEDIUM severity within blast cap",
                    labels=labels,
                )
            case Severity.LOW:
                # LOW with no impact ⇒ alert only (skip noisy PRs).
                if not self._has_any_impact(impact):
                    return PolicyDecision(
                        action=Action.ALERT_ONLY,
                        reason="LOW severity with no downstream impact",
                        labels=labels,
                    )
                self._record()
                if large_blast:
                    return PolicyDecision(
                        action=Action.OPEN_DRAFT_PR,
                        reason=(
                            f"LOW severity but blast_radius "
                            f"{impact.blast_radius_score:.1f} > cap {self.blast_radius_cap:.1f}"
                        ),
                        labels=(*labels, "requires-human", "large-blast"),
                    )
                return PolicyDecision(
                    action=Action.OPEN_PR,
                    reason="LOW severity with downstream impact",
                    labels=labels,
                )
        # mypy: match is exhaustive but match-on-StrEnum isn't proven so.
        raise AssertionError(f"unhandled severity {event.severity!r}")  # pragma: no cover

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _kill_switch_active() -> bool:
        return os.environ.get(KILL_SWITCH_ENV, "").strip() in {"1", "true", "TRUE", "yes"}

    def _within_rate_limit(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        return len(self._recent) < self.max_events_per_window

    def _record(self) -> None:
        self._recent.append(time.monotonic())

    @staticmethod
    def _has_any_impact(impact: ImpactSet) -> bool:
        return bool(
            impact.dbt_models or impact.affected_columns or impact.dashboards or impact.ml_features
        )

    @staticmethod
    def _base_labels(event: DriftEvent, impact: ImpactSet) -> tuple[str, ...]:
        base = [f"drift:{event.change_type.value}", f"severity:{event.severity.value}"]
        if impact.fan_out_conservative:
            base.append("fan-out-widened")
        return tuple(base)


__all__ = ["KILL_SWITCH_ENV", "PolicyDecision", "PolicyEngine"]
