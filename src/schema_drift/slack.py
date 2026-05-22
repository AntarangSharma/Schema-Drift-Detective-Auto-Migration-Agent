"""Slack notifier (Block Kit).

POSTs a Block Kit payload to ``DRIFT_SLACK_WEBHOOK_URL``. No-op when
the env var is unset — same contract as the OpenLineage emitter.

Why Block Kit and not plain text
--------------------------------
Drift events have structure (severity, change type, model list, PR
link). Block Kit keeps the structure on the rendered side, so an
on-call can spot a HIGH-severity destructive change in a glance
without reading the body.

Failure mode
------------
Network errors are logged and swallowed — a Slack outage must not
break the drift pipeline. The caller still has the ``AuditRecord``
to fall back on.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from schema_drift.models import Action, DriftEvent, ImpactSet, Severity

SLACK_WEBHOOK_ENV = "DRIFT_SLACK_WEBHOOK_URL"
DEFAULT_TIMEOUT = 3.0

_log = logging.getLogger(__name__)

_SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.LOW: ":large_blue_circle:",
    Severity.MEDIUM: ":large_yellow_circle:",
    Severity.HIGH: ":red_circle:",
}

_ACTION_LABEL: dict[Action, str] = {
    Action.IGNORE: "ignored",
    Action.ALERT_ONLY: "alerted (no PR)",
    Action.OPEN_DRAFT_PR: "drafted PR (needs review)",
    Action.OPEN_PR: "opened PR",
}


@dataclass(slots=True)
class SlackConfig:
    webhook_url: str | None
    timeout_seconds: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> SlackConfig:
        return cls(webhook_url=os.environ.get(SLACK_WEBHOOK_ENV) or None)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)


class SlackNotifier:
    """Stateless Block-Kit POSTer."""

    def __init__(
        self, config: SlackConfig | None = None, *, client: httpx.Client | None = None
    ) -> None:
        self._config = config or SlackConfig.from_env()
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def notify(
        self,
        event: DriftEvent,
        action: Action,
        *,
        pr_url: str | None = None,
        reason: str | None = None,
        impact: ImpactSet | None = None,
    ) -> bool:
        if not self._config.enabled:
            return False
        payload = self.build_payload(event, action, pr_url=pr_url, reason=reason, impact=impact)
        client = self._client or httpx.Client(timeout=self._config.timeout_seconds)
        owns_client = self._client is None
        try:
            resp = client.post(self._config.webhook_url, json=payload)  # type: ignore[arg-type]
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:  # pragma: no cover -- network path
            _log.warning("Slack notify failed for %s: %s", event.id, exc)
            return False
        finally:
            if owns_client:
                client.close()

    # ------------------------------------------------------------------ #
    # Block builder (pure, easy to unit-test)                             #
    # ------------------------------------------------------------------ #

    def build_payload(
        self,
        event: DriftEvent,
        action: Action,
        *,
        pr_url: str | None = None,
        reason: str | None = None,
        impact: ImpactSet | None = None,
    ) -> dict[str, Any]:
        impact = impact or event.impact
        emoji = _SEVERITY_EMOJI.get(event.severity, ":grey_question:")
        header = f"{emoji} Schema drift: {event.change_type.value}"

        fields: list[dict[str, str]] = [
            {"type": "mrkdwn", "text": f"*Source*\n`{event.source_identifier}`"},
            {"type": "mrkdwn", "text": f"*Severity*\n{event.severity.value}"},
            {"type": "mrkdwn", "text": f"*Action*\n{_ACTION_LABEL[action]}"},
            {"type": "mrkdwn", "text": f"*Confidence*\n{event.confidence:.2f}"},
        ]
        if impact.dbt_models:
            preview = ", ".join(impact.dbt_models[:5])
            if len(impact.dbt_models) > 5:
                preview += f" (+{len(impact.dbt_models) - 5} more)"
            fields.append({"type": "mrkdwn", "text": f"*Affected models*\n{preview}"})
        if impact.blast_radius_score:
            fields.append(
                {"type": "mrkdwn", "text": f"*Blast radius*\n{impact.blast_radius_score:.1f}"}
            )

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": header}},
            {"type": "section", "fields": fields},
        ]
        if reason:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f":memo: {reason}"}]}
            )
        if pr_url:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Open PR"},
                            "url": pr_url,
                        }
                    ],
                }
            )

        return {"text": header, "blocks": blocks}


__all__ = ["SLACK_WEBHOOK_ENV", "SlackConfig", "SlackNotifier"]
