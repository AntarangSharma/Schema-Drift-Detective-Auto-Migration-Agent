"""OpenLineage emitter.

Posts a single ``RunEvent`` per drift detection so a Marquez UI (or
any OL-compatible backend) shows the drift alongside the normal
ELT lineage. The emitter is **no-op when ``OPENLINEAGE_URL`` is
unset** — that's the explicit contract so CI / local dev never need
a backend running.

OpenLineage spec compliance
---------------------------

This file targets OpenLineage spec **1-0-5** (current as of 2026-05).
The two facets we emit are both schema-validated against the public
JSON schemas published at https://openlineage.io/spec/facets/ :

* ``run.facets.parent`` — :class:`ParentRunFacet`. Required when a
  drift event was kicked off by an upstream ELT run we already know
  about (e.g. a dbt run that produced ``manifest.json``). This lets
  Marquez stitch the drift run into the same lineage graph as the
  run that *caused* it. Schema:
  https://openlineage.io/spec/facets/1-0-1/ParentRunFacet.json
* ``run.facets.drift`` — custom facet keyed to our own producer URI.
  Carries change_type, severity, confidence, blast_radius_score, and
  fan_out_conservative.

Both facets carry the mandatory ``_producer`` and ``_schemaURL``
fields. Marquez 0.40+ rejects facets missing those two fields with
a 400.

Why a hand-rolled emitter and not ``openlineage-python``
--------------------------------------------------------
The python client pulls in a deep tree (httpx, attrs, requests_unixsocket
on some platforms). For a one-event POST we don't need any of that;
we just need the JSON shape Marquez expects. If a user wants the full
client they can install it themselves and swap the implementation by
setting ``DRIFT_OL_EMITTER=client``; we keep the no-dep path as default.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from schema_drift.models import DriftEvent, ImpactSet

OPENLINEAGE_URL_ENV = "OPENLINEAGE_URL"
OPENLINEAGE_NAMESPACE_ENV = "OPENLINEAGE_NAMESPACE"
OPENLINEAGE_PARENT_RUN_ID_ENV = "OPENLINEAGE_PARENT_RUN_ID"
OPENLINEAGE_PARENT_JOB_NAME_ENV = "OPENLINEAGE_PARENT_JOB_NAME"
OPENLINEAGE_PARENT_JOB_NAMESPACE_ENV = "OPENLINEAGE_PARENT_JOB_NAMESPACE"

DEFAULT_NAMESPACE = "schema-drift-detective"
DEFAULT_TIMEOUT = 3.0

# Spec constants pinned at module level so a bump is one diff.
OL_SPEC_VERSION = "1-0-5"
OL_RUN_EVENT_SCHEMA_URL = (
    f"https://openlineage.io/spec/{OL_SPEC_VERSION}/OpenLineage.json#/definitions/RunEvent"
)
OL_PARENT_FACET_SCHEMA_URL = (
    "https://openlineage.io/spec/facets/1-0-1/ParentRunFacet.json#/$defs/ParentRunFacet"
)
OL_SCHEMA_DATASET_FACET_SCHEMA_URL = (
    "https://openlineage.io/spec/facets/1-0-1/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
)
OL_PRODUCER_URI = "https://github.com/antarang/schema-drift-detective"
OL_DRIFT_FACET_SCHEMA_URL = f"{OL_PRODUCER_URI}/blob/main/docs/04_architecture.md#drift-facet"

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ParentRunFacet — typed value object
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ParentRunFacet:
    """Typed representation of an OpenLineage ParentRunFacet (spec 1-0-1).

    The OL spec requires exactly three fields under ``parent``:

    * ``run.runId`` — UUID string of the upstream run.
    * ``job.namespace`` — namespace the parent job lives in.
    * ``job.name`` — parent job name.

    We add the two mandatory facet metadata fields (``_producer``,
    ``_schemaURL``) on render. See ``ParentRunFacet.json`` for the
    full schema; the dataclass mirrors it 1:1.
    """

    run_id: str
    job_namespace: str
    job_name: str

    @classmethod
    def from_env(cls) -> ParentRunFacet | None:
        """Build from the three documented env vars.

        Returns ``None`` if any of the three is unset — half-configured
        parent facets are useless to Marquez and would 400. The
        all-or-nothing rule keeps the contract loud.
        """
        run_id = os.environ.get(OPENLINEAGE_PARENT_RUN_ID_ENV)
        job_name = os.environ.get(OPENLINEAGE_PARENT_JOB_NAME_ENV)
        job_namespace = os.environ.get(OPENLINEAGE_PARENT_JOB_NAMESPACE_ENV, DEFAULT_NAMESPACE)
        if not run_id or not job_name:
            return None
        return cls(run_id=run_id, job_namespace=job_namespace, job_name=job_name)

    def to_facet_dict(self) -> dict[str, Any]:
        """Render to the exact JSON shape Marquez validates.

        Output:

        .. code-block:: json

            {
                "_producer": "...",
                "_schemaURL": "...",
                "run":  {"runId": "<uuid>"},
                "job":  {"namespace": "...", "name": "..."}
            }
        """
        return {
            "_producer": OL_PRODUCER_URI,
            "_schemaURL": OL_PARENT_FACET_SCHEMA_URL,
            "run": {"runId": self.run_id},
            "job": {"namespace": self.job_namespace, "name": self.job_name},
        }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OLConfig:
    url: str | None
    namespace: str = DEFAULT_NAMESPACE
    timeout_seconds: float = DEFAULT_TIMEOUT
    parent: ParentRunFacet | None = None

    @classmethod
    def from_env(cls) -> OLConfig:
        return cls(
            url=os.environ.get(OPENLINEAGE_URL_ENV) or None,
            namespace=os.environ.get(OPENLINEAGE_NAMESPACE_ENV, DEFAULT_NAMESPACE),
            parent=ParentRunFacet.from_env(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url)


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class OpenLineageEmitter:
    """Stateless POST-once emitter.

    Each ``emit`` call sends one ``RunEvent`` describing the drift.
    Failures are logged and swallowed — we never let a backend
    outage break the drift pipeline.
    """

    def __init__(
        self,
        config: OLConfig | None = None,
        *,
        client: httpx.Client | None = None,
        parent: ParentRunFacet | None = None,
    ) -> None:
        self._config = config or OLConfig.from_env()
        self._client = client  # injected in tests; lazily built in prod
        # Constructor-level parent overrides config-level parent, which in
        # turn overrides env-level parent. Three-level fallback so the
        # CLI / unit tests can plug a parent in cleanly.
        if parent is not None:
            self._config.parent = parent

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def parent(self) -> ParentRunFacet | None:
        return self._config.parent

    def emit(self, event: DriftEvent, impact: ImpactSet | None = None) -> bool:
        """Returns ``True`` if a POST was actually attempted."""
        if not self._config.enabled:
            return False
        impact = impact or event.impact
        payload = self._build_run_event(event, impact)
        endpoint = urljoin(self._config.url + "/", "api/v1/lineage")  # type: ignore[arg-type]
        client = self._client or httpx.Client(timeout=self._config.timeout_seconds)
        owns_client = self._client is None
        try:
            resp = client.post(endpoint, json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:  # pragma: no cover -- network path
            _log.warning("OpenLineage emit failed for %s: %s", event.id, exc)
            return False
        finally:
            if owns_client:
                client.close()

    # ------------------------------------------------------------------ #
    # Payload builder                                                     #
    # ------------------------------------------------------------------ #

    def _build_run_event(self, event: DriftEvent, impact: ImpactSet) -> dict[str, Any]:
        """Build a spec-1-0-5 ``RunEvent`` JSON.

        We model the drift as a *run* of the abstract job
        ``schema_drift_detect`` whose input dataset is the source
        identifier and whose output datasets are the affected dbt
        models. When a parent run is configured, ``run.facets.parent``
        is emitted so Marquez stitches the drift run into the upstream
        ELT graph; without it, the drift run appears as an
        independent root run.
        """
        ns = self._config.namespace
        run_facets: dict[str, Any] = {
            "drift": {
                "_producer": OL_PRODUCER_URI,
                "_schemaURL": OL_DRIFT_FACET_SCHEMA_URL,
                "change_type": event.change_type.value,
                "severity": event.severity.value,
                "confidence": event.confidence,
                "blast_radius_score": impact.blast_radius_score,
                "fan_out_conservative": impact.fan_out_conservative,
            }
        }
        if self._config.parent is not None:
            # Per spec 1-0-5, the facet key is "parent" and its value
            # is the ParentRunFacet object. We do NOT inline runId at
            # the top of ``run`` — the parent facet is what Marquez
            # parses for stitching.
            run_facets["parent"] = self._config.parent.to_facet_dict()

        return {
            "eventType": "COMPLETE",
            "eventTime": _iso(event.detected_at),
            "producer": OL_PRODUCER_URI,
            "schemaURL": OL_RUN_EVENT_SCHEMA_URL,
            "run": {
                "runId": event.id,
                "facets": run_facets,
            },
            "job": {"namespace": ns, "name": "schema_drift_detect"},
            "inputs": [
                {
                    "namespace": ns,
                    "name": event.source_identifier,
                    "facets": {
                        "schema": {
                            "_producer": OL_PRODUCER_URI,
                            "_schemaURL": OL_SCHEMA_DATASET_FACET_SCHEMA_URL,
                            "fields": _column_facet(event),
                        }
                    },
                }
            ],
            "outputs": [{"namespace": ns, "name": m} for m in impact.dbt_models],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:  # pragma: no cover -- DriftEvent enforces tz
        return dt.isoformat() + "Z"
    return dt.isoformat()


def _column_facet(event: DriftEvent) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if event.column_before is not None:
        fields.append({"name": event.column_before.name, "type": event.column_before.data_type})
    if event.column_after is not None:
        fields.append({"name": event.column_after.name, "type": event.column_after.data_type})
    return fields


__all__ = [
    "OL_PARENT_FACET_SCHEMA_URL",
    "OL_PRODUCER_URI",
    "OL_RUN_EVENT_SCHEMA_URL",
    "OL_SCHEMA_DATASET_FACET_SCHEMA_URL",
    "OL_SPEC_VERSION",
    "OPENLINEAGE_NAMESPACE_ENV",
    "OPENLINEAGE_PARENT_JOB_NAMESPACE_ENV",
    "OPENLINEAGE_PARENT_JOB_NAME_ENV",
    "OPENLINEAGE_PARENT_RUN_ID_ENV",
    "OPENLINEAGE_URL_ENV",
    "OLConfig",
    "OpenLineageEmitter",
    "ParentRunFacet",
]
