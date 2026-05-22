"""Metabase adapter.

Maps dbt model names → Metabase cards (questions / dashboards) so
``ImpactSet.dashboards`` can show "this drift breaks the Exec
Revenue dashboard, not just an obscure mart". The mapping is built
by walking ``/api/card`` and matching the card's ``dataset_query``
to the model name.

No-op default
-------------
The adapter follows the same contract as ``slack.py`` and ``ol.py``:
no ``METABASE_URL`` ⇒ ``enabled = False`` ⇒ every lookup returns
``()``. Wiring it up is purely additive — a workspace without
Metabase pays zero cost.

API surface
-----------
* ``MetabaseAdapter.fetch_dashboards()`` — fetch + cache the model→cards
  map (one HTTP call per refresh).
* ``MetabaseAdapter.dashboards_for(model_name)`` — returns
  ``tuple[DashboardRef, ...]`` for a single model; the lineage code
  calls this per impacted model.

Tiering
-------
We classify a card as ``critical`` if its name matches any pattern in
``critical_patterns`` (default: ``"exec"``, ``"board"``, ``"finance"``).
Most shops want to override this with their own list.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from schema_drift.models import DashboardRef

METABASE_URL_ENV = "METABASE_URL"
METABASE_API_KEY_ENV = "METABASE_API_KEY"
DEFAULT_TIMEOUT = 5.0

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class MetabaseConfig:
    url: str | None
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT
    critical_patterns: tuple[str, ...] = ("exec", "board", "finance")

    @classmethod
    def from_env(cls) -> MetabaseConfig:
        return cls(
            url=os.environ.get(METABASE_URL_ENV) or None,
            api_key=os.environ.get(METABASE_API_KEY_ENV) or None,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url)


@dataclass(slots=True)
class MetabaseAdapter:
    """Fetch + cache Metabase card metadata.

    Stateful: ``_model_to_cards`` is built on first call and then
    served from memory. Call ``refresh()`` to invalidate.
    """

    config: MetabaseConfig = field(default_factory=MetabaseConfig.from_env)
    client: httpx.Client | None = None
    _model_to_cards: dict[str, list[DashboardRef]] = field(
        default_factory=dict, init=False, repr=False
    )
    _loaded: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------ #
    # Public                                                              #
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def dashboards_for(self, model_name: str) -> tuple[DashboardRef, ...]:
        """Return dashboards (cards) using this model.

        Default-safe: when the adapter is disabled, returns ``()``.
        """
        if not self.enabled:
            return ()
        if not self._loaded:
            self.fetch_dashboards()
        return tuple(self._model_to_cards.get(model_name, ()))

    def refresh(self) -> None:
        self._loaded = False
        self._model_to_cards.clear()

    def fetch_dashboards(self) -> dict[str, tuple[DashboardRef, ...]]:
        """Pull all cards from Metabase, group by referenced model name.

        Best-effort: any HTTP / parse failure logs and returns ``{}``.
        """
        if not self.enabled:
            return {}
        owns_client = self.client is None
        client = self.client or httpx.Client(
            timeout=self.config.timeout_seconds,
            headers=self._auth_headers(),
        )
        try:
            # Pass auth headers per-request as well, so an injected client
            # (e.g. an httpx.Client created with a MockTransport in tests)
            # still carries the API-key header even though it wasn't
            # constructed with the default headers above.
            cards = self._fetch_cards(client, headers=self._auth_headers())
            self._model_to_cards = self._group_by_model(cards)
            self._loaded = True
            return {k: tuple(v) for k, v in self._model_to_cards.items()}
        except httpx.HTTPError as exc:  # pragma: no cover -- network path
            _log.warning("Metabase fetch failed: %s", exc)
            return {}
        finally:
            if owns_client:
                client.close()

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _auth_headers(self) -> dict[str, str]:
        if self.config.api_key:
            return {"X-API-KEY": self.config.api_key}
        return {}

    def _fetch_cards(
        self,
        client: httpx.Client,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        # Lower-case the header name so the assertion ``headers.get("x-api-key")``
        # in the test (httpx normalises headers to lower-case) sees it.
        outgoing = {k.lower(): v for k, v in (headers or {}).items()} or None
        resp = client.get(f"{self.config.url}/api/card", headers=outgoing)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):  # pragma: no cover -- defensive
            return []
        return data

    def _group_by_model(self, cards: list[dict[str, Any]]) -> dict[str, list[DashboardRef]]:
        out: dict[str, list[DashboardRef]] = {}
        for card in cards:
            model_names = _extract_model_refs(card)
            if not model_names:
                continue
            ref = self._card_to_ref(card)
            for m in model_names:
                out.setdefault(m, []).append(ref)
        return out

    def _card_to_ref(self, card: dict[str, Any]) -> DashboardRef:
        name = str(card.get("name", "unnamed card"))
        cid = str(card.get("id", "?"))
        url = f"{self.config.url}/question/{cid}" if self.config.url else None
        tier: str = "normal"
        lowered = name.lower()
        for pat in self.config.critical_patterns:
            if pat in lowered:
                tier = "critical"
                break
        return DashboardRef(
            tool="metabase",
            id=cid,
            name=name,
            tier=tier,  # type: ignore[arg-type]
            url=url,
        )


# ---------------------------------------------------------------------------
# Model-name extraction
# ---------------------------------------------------------------------------


def _extract_model_refs(card: dict[str, Any]) -> list[str]:
    """Best-effort model-name extraction from a Metabase ``card`` payload.

    Metabase stores either:
    * ``dataset_query.query.source-table`` (table reference by ID, no name) -
      ignored here; the caller would need to walk Metabase's ``/api/table``.
    * ``dataset_query.native.query`` (raw SQL) — we grep for ``FROM
      <schema>.<table>`` references. This is good enough for the demo;
      production deployments substitute their own extractor.
    """
    dq = card.get("dataset_query") or {}
    native = (dq.get("native") or {}).get("query")
    if not isinstance(native, str):
        return []
    return _grep_from_tables(native)


def _grep_from_tables(sql: str) -> list[str]:
    """Naïve ``FROM`` / ``JOIN`` extractor — splits on whitespace and picks
    the next token. Returns the *unqualified* table name (last
    dot-separated segment).
    """
    out: list[str] = []
    tokens = sql.replace("\n", " ").replace(",", " ").split()
    upper = [t.upper().strip("();") for t in tokens]
    for i, tok in enumerate(upper):
        if tok in ("FROM", "JOIN") and i + 1 < len(tokens):
            raw = tokens[i + 1].strip("();,")
            if not raw or raw.startswith("("):
                continue
            short = raw.split(".")[-1].strip('"`')
            if short and short.lower() not in out:
                out.append(short.lower())
    return out


__all__ = ["METABASE_URL_ENV", "MetabaseAdapter", "MetabaseConfig"]
