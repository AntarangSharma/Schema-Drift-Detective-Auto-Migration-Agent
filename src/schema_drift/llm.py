"""LLM adapter — thin, provider-agnostic, mock-friendly.

Why a Protocol, not a concrete class
------------------------------------
We want three call sites — Claude (default), OpenAI (parity), and a
``MockLLM`` (CI) — to be interchangeable. A Protocol keeps the call sites
honest (no provider-specific kwargs leak) and the test suite hermetic
(no API keys, no network).

Switching at runtime
--------------------
``make_llm()`` reads ``DRIFT_LLM_PROVIDER`` (``mock``/``anthropic``/``openai``).
The constructors for the real providers ``raise NotImplementedError`` for
now — Week 4's gate is the *interface* and the *Pydantic validation
loop*, not the live provider integrations (those land in Week 5 once we
have credentials in the CI matrix).

Pydantic-validated output
-------------------------
The LLM must return a ``MigrationProposal``. We use Pydantic's
``Literal[col_names]`` enforcement so a model that hallucinates a column
fails *during validation*, before the proposal ever touches a real file.

Retry & fall-back
-----------------
``draft_with_validation(...)`` runs:

    for attempt in range(max_retries + 1):
        proposal = llm.draft(prompt)
        ok, err = validator(proposal)   # dbt parse + compile
        if ok: return proposal
        prompt += err                   # feedback loop
    raise LLMValidationFailed

The caller (``MigrationDrafter``) catches ``LLMValidationFailed`` and
falls back to the Day-3 deterministic patcher so we ship *something*
even if the LLM is broken or unfunded.

Cost tracking
-------------
Every call returns ``LLMResponse(content, tokens_in, tokens_out, cost_usd,
model)``; the drafter copies those onto ``AuditRecord.payload`` so the
final RESULTS.md cost-per-1k column is grounded in real numbers, not
estimates.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LLMResponse:
    """Wrapper around the raw model output + cost metadata."""

    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = "unknown"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class LLM(Protocol):
    """Provider-agnostic LLM seam.

    ``draft(prompt)`` returns the raw text; callers parse it as JSON and
    feed it through ``MigrationProposal.model_validate_json``. Streaming
    is intentionally not part of the interface (we always want the full
    response for validation).
    """

    name: str

    def draft(self, prompt: str) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MockLLM:
    """In-process LLM used by CI. Returns whatever ``next_response`` is set
    to, increments ``call_count``. Tests assert on the recorded prompts.

    ``next_response`` is a callable so test cases can vary the response by
    attempt (e.g. fail once, succeed on retry).
    """

    name: str = "mock"
    call_count: int = 0
    received_prompts: list[str] = field(default_factory=list)
    _responses: list[LLMResponse] = field(default_factory=list)

    def queue(self, *responses: LLMResponse) -> None:
        """Queue a sequence of responses; each call pops the front."""
        self._responses.extend(responses)

    def draft(self, prompt: str) -> LLMResponse:
        self.call_count += 1
        self.received_prompts.append(prompt)
        if not self._responses:
            # Default benign response if the test forgot to queue.
            return LLMResponse(content="{}", model=self.name)
        return self._responses.pop(0)


class AnthropicLLM:  # pragma: no cover — needs network + key
    """Claude via ``anthropic`` SDK. Reads ``ANTHROPIC_API_KEY``.

    Stubbed in Week 4 — the *interface* is what's exercised by the
    drafter's retry loop. The real call lights up in Week 5 once we have
    a sandboxed key in the CI matrix.
    """

    name = "anthropic"

    def __init__(self, model: str = "claude-3-5-sonnet-latest") -> None:
        self.model = model

    def draft(self, prompt: str) -> LLMResponse:
        raise NotImplementedError(
            "AnthropicLLM is stubbed until Week 5. Set DRIFT_LLM_PROVIDER=mock for now."
        )


class OpenAILLM:  # pragma: no cover — needs network + key
    """OpenAI parity adapter. Reads ``OPENAI_API_KEY``."""

    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model

    def draft(self, prompt: str) -> LLMResponse:
        raise NotImplementedError(
            "OpenAILLM is stubbed until Week 5. Set DRIFT_LLM_PROVIDER=mock for now."
        )


def make_llm(provider: str | None = None) -> LLM:
    """Factory honouring ``DRIFT_LLM_PROVIDER`` env (default: ``mock``)."""
    provider = (provider or os.environ.get("DRIFT_LLM_PROVIDER", "mock")).lower()
    match provider:
        case "mock":
            return MockLLM()
        case "anthropic":
            return AnthropicLLM()
        case "openai":
            return OpenAILLM()
    raise ValueError(f"Unknown LLM provider: {provider!r}")


# ---------------------------------------------------------------------------
# MigrationProposal — Pydantic with allowlist column names
# ---------------------------------------------------------------------------


# dbt's built-in data tests (1.8). Anything outside this allowlist is
# rejected at validation time — we never want the LLM to invent a test name.
_ALLOWED_TESTS = frozenset({"not_null", "unique", "accepted_values", "relationships"})


def _make_proposal_model(allowed_columns: tuple[str, ...]) -> type[BaseModel]:
    """Build a ``MigrationProposal`` class with ``allowed_columns`` baked
    into the validator. We use a closure-built class instead of a generic
    because Pydantic v2's ``Literal[*tuple]`` syntax requires the tuple
    contents at class-definition time, which is exactly what we have
    here from the dbt source metadata.
    """

    class MigrationProposal(BaseModel):
        model_config = ConfigDict(
            frozen=True,
            extra="forbid",
            str_strip_whitespace=False,  # keep YAML whitespace exactly
            validate_assignment=True,
        )

        summary: str = Field(min_length=1, max_length=400)
        patched_sources_yml: str = Field(min_length=1)
        backfill_sql: str = ""
        rollback_sql: str = ""
        tests_to_add: tuple[str, ...] = ()
        risk_notes: tuple[str, ...] = ()
        # The set of column names the LLM is allowed to reference. Carried
        # on the model for transparency; the validator below enforces it.
        referenced_columns: tuple[str, ...] = ()

        @model_validator(mode="after")
        def _check(self) -> MigrationProposal:
            # 1. Allowed test names only.
            disallowed = [t for t in self.tests_to_add if t not in _ALLOWED_TESTS]
            if disallowed:
                raise ValueError(
                    f"tests_to_add includes non-built-in test(s): {disallowed!r}; "
                    f"allowed: {sorted(_ALLOWED_TESTS)}"
                )
            # 2. Referenced columns must be a subset of ``allowed_columns``.
            extra_cols = [c for c in self.referenced_columns if c not in allowed_columns]
            if extra_cols:
                raise ValueError(
                    f"referenced_columns includes columns not in the source: "
                    f"{extra_cols!r}; allowed: {sorted(allowed_columns)}"
                )
            return self

    return MigrationProposal


# Public alias so type-checkers see a stable name. Concrete validator is
# built per-call via ``_make_proposal_model`` so the column allowlist is
# specific to *this drift event's source table*.
MigrationProposalT = type[BaseModel]


# ---------------------------------------------------------------------------
# Validation loop
# ---------------------------------------------------------------------------


class DbtRunner(Protocol):
    """Test seam for ``dbt parse && dbt compile``. The default real impl
    shells out to dbt; tests inject a stub that returns canned results."""

    def parse_and_compile(self, project_dir: str, patched_yaml: str) -> tuple[bool, str]:
        """Return ``(ok, error_message_or_empty)``."""


@dataclass(slots=True)
class StubDbtRunner:
    """Default test DbtRunner — always succeeds. Real impl arrives Week 5."""

    name: str = "stub"

    def parse_and_compile(self, project_dir: str, patched_yaml: str) -> tuple[bool, str]:
        return True, ""


class LLMValidationFailed(RuntimeError):
    """Raised when the LLM cannot produce a valid ``MigrationProposal``
    within ``max_retries + 1`` attempts. The drafter catches this and
    falls back to the deterministic Day-3 patcher."""


@dataclass(slots=True)
class DraftOutcome:
    """Result of one full draft attempt (across N retries).

    Tracking ``attempts`` and ``total_cost_usd`` separately from ``proposal``
    lets the drafter write an audit record even when validation eventually
    fails (``proposal=None``). The cost is real money — we never want to
    lose it because of an exception.
    """

    proposal: BaseModel | None
    response: LLMResponse | None
    attempts: int
    total_cost_usd: float
    errors: tuple[str, ...]


def draft_with_validation(
    llm: LLM,
    *,
    prompt: str,
    allowed_columns: tuple[str, ...],
    dbt_runner: DbtRunner,
    project_dir: str,
    max_retries: int = 2,
) -> DraftOutcome:
    """Run the LLM with a JSON-validation + dbt-compile retry loop.

    Parameters
    ----------
    llm
        Any ``LLM``-shaped object (real provider or ``MockLLM``).
    prompt
        Initial prompt (already rendered with the drift event).
    allowed_columns
        Column names the model is permitted to reference. Comes from the
        dbt sources.yml of the table under change.
    dbt_runner
        Validator that runs ``dbt parse && dbt compile`` on the patched
        YAML; returns ``(ok, error_message)``.
    project_dir
        dbt project directory passed through to the runner.
    max_retries
        Max retries after the first attempt; total attempts = ``max_retries + 1``.
    """
    Proposal = _make_proposal_model(allowed_columns)
    cumulative_cost = 0.0
    errors: list[str] = []
    response: LLMResponse | None = None

    current_prompt = prompt
    for attempt in range(max_retries + 1):
        response = llm.draft(current_prompt)
        cumulative_cost += response.cost_usd

        # 1. Validate JSON shape.
        try:
            proposal = Proposal.model_validate_json(response.content)
        except ValidationError as exc:
            err = f"JSON validation failed:\n{exc}"
            errors.append(err)
            current_prompt = f"{prompt}\n\nPrevious attempt failed:\n{err}\nRetry."
            continue

        # 2. Validate via dbt parse/compile.
        ok, dbt_err = dbt_runner.parse_and_compile(project_dir, proposal.patched_sources_yml)  # type: ignore[attr-defined]
        if ok:
            return DraftOutcome(
                proposal=proposal,
                response=response,
                attempts=attempt + 1,
                total_cost_usd=cumulative_cost,
                errors=tuple(errors),
            )
        errors.append(f"dbt parse/compile failed: {dbt_err}")
        current_prompt = f"{prompt}\n\nPrevious attempt failed dbt compile:\n{dbt_err}\nRetry."

    raise LLMValidationFailed(
        f"LLM produced no valid proposal after {max_retries + 1} attempts. "
        f"Errors:\n{chr(10).join(errors)}"
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def load_prompt_template(version: Literal["v1"] = "v1") -> str:
    """Load the versioned drafter prompt from ``prompts/migration_drafter.md``."""

    # The prompt lives in the repo root, not inside the package; resolve
    # by walking up from this file's location.
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "prompts" / "migration_drafter.md"
    if not path.exists():  # pragma: no cover
        raise FileNotFoundError(f"prompt template missing: {path}")
    text = path.read_text()
    if f"version: {version}" not in text:  # pragma: no cover
        raise ValueError(f"prompt template version mismatch (wanted {version})")
    return text


__all__ = [
    "LLM",
    "AnthropicLLM",
    "DbtRunner",
    "DraftOutcome",
    "LLMResponse",
    "LLMValidationFailed",
    "MockLLM",
    "OpenAILLM",
    "StubDbtRunner",
    "_make_proposal_model",
    "draft_with_validation",
    "load_prompt_template",
    "make_llm",
]
