"""Tests for the LLM adapter + Pydantic-validated MigrationProposal."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest
from pydantic import ValidationError

from schema_drift.llm import (
    DraftOutcome,
    LLMResponse,
    LLMValidationFailed,
    MockLLM,
    StubDbtRunner,
    _make_proposal_model,
    draft_with_validation,
    load_prompt_template,
    make_llm,
)


@pytest.fixture
def allowed():
    return ("order_id", "amount", "discount_code", "status")


@pytest.fixture
def proposal_cls(allowed):
    return _make_proposal_model(allowed)


# ---------------------------------------------------------------------------
# MockLLM
# ---------------------------------------------------------------------------


class TestMockLLM:
    def test_records_prompt_and_returns_queued_response(self):
        llm = MockLLM()
        llm.queue(LLMResponse(content="hello", tokens_in=1, tokens_out=2))
        out = llm.draft("p1")
        assert out.content == "hello"
        assert llm.received_prompts == ["p1"]
        assert llm.call_count == 1

    def test_default_response_when_queue_empty(self):
        llm = MockLLM()
        out = llm.draft("p")
        assert out.content == "{}"


# ---------------------------------------------------------------------------
# MigrationProposal validator
# ---------------------------------------------------------------------------


class TestMigrationProposal:
    def test_happy_path(self, proposal_cls):
        p = proposal_cls.model_validate_json(
            json.dumps(
                {
                    "summary": "Add discount_code to sources.yml.",
                    "patched_sources_yml": "version: 2\nsources: []\n",
                    "tests_to_add": ["not_null"],
                    "referenced_columns": ["discount_code"],
                }
            )
        )
        assert p.summary.startswith("Add")
        assert p.tests_to_add == ("not_null",)

    def test_rejects_unknown_test(self, proposal_cls):
        with pytest.raises(ValidationError, match="non-built-in test"):
            proposal_cls.model_validate_json(
                json.dumps(
                    {
                        "summary": "x",
                        "patched_sources_yml": "y",
                        "tests_to_add": ["assert_truth"],
                    }
                )
            )

    def test_rejects_invented_column(self, proposal_cls):
        with pytest.raises(ValidationError, match="columns not in the source"):
            proposal_cls.model_validate_json(
                json.dumps(
                    {
                        "summary": "x",
                        "patched_sources_yml": "y",
                        "referenced_columns": ["hallucinated"],
                    }
                )
            )

    def test_rejects_extra_field(self, proposal_cls):
        with pytest.raises(ValidationError):
            proposal_cls.model_validate_json(
                json.dumps(
                    {
                        "summary": "x",
                        "patched_sources_yml": "y",
                        "secret": "leak",
                    }
                )
            )


# ---------------------------------------------------------------------------
# Validation loop
# ---------------------------------------------------------------------------


@dataclass
class _FailingDbtRunner:
    fail_count: int = 1
    seen: int = 0

    def parse_and_compile(self, project_dir: str, patched_yaml: str) -> tuple[bool, str]:
        self.seen += 1
        if self.seen <= self.fail_count:
            return False, "boom: column foo missing"
        return True, ""


class TestValidationLoop:
    def _good_response(self) -> LLMResponse:
        return LLMResponse(
            content=json.dumps(
                {
                    "summary": "ok",
                    "patched_sources_yml": "version: 2\n",
                    "tests_to_add": ["not_null"],
                    "referenced_columns": ["order_id"],
                }
            ),
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
            model="mock",
        )

    def test_happy_first_attempt(self, allowed):
        llm = MockLLM()
        llm.queue(self._good_response())
        out = draft_with_validation(
            llm,
            prompt="P",
            allowed_columns=allowed,
            dbt_runner=StubDbtRunner(),
            project_dir="/tmp",
        )
        assert out.proposal is not None
        assert out.attempts == 1
        assert out.total_cost_usd == pytest.approx(0.001)
        assert llm.call_count == 1

    def test_retries_on_json_validation_failure(self, allowed):
        llm = MockLLM()
        # First: invalid (missing required fields). Second: good.
        llm.queue(
            LLMResponse(content="{}", cost_usd=0.0005),
            self._good_response(),
        )
        out = draft_with_validation(
            llm,
            prompt="P",
            allowed_columns=allowed,
            dbt_runner=StubDbtRunner(),
            project_dir="/tmp",
        )
        assert out.attempts == 2
        # Second prompt has the error feedback appended.
        assert "Previous attempt failed" in llm.received_prompts[1]
        # Cost accumulated across attempts.
        assert out.total_cost_usd == pytest.approx(0.0005 + 0.001)

    def test_retries_on_dbt_compile_failure(self, allowed):
        llm = MockLLM()
        llm.queue(self._good_response(), self._good_response())
        runner = _FailingDbtRunner(fail_count=1)
        out = draft_with_validation(
            llm,
            prompt="P",
            allowed_columns=allowed,
            dbt_runner=runner,
            project_dir="/tmp",
        )
        assert out.attempts == 2
        assert "Previous attempt failed dbt compile" in llm.received_prompts[1]

    def test_gives_up_after_max_retries(self, allowed):
        llm = MockLLM()
        # All three attempts fail validation.
        llm.queue(
            LLMResponse(content="{}"),
            LLMResponse(content="{}"),
            LLMResponse(content="{}"),
        )
        with pytest.raises(LLMValidationFailed, match="3 attempts"):
            draft_with_validation(
                llm,
                prompt="P",
                allowed_columns=allowed,
                dbt_runner=StubDbtRunner(),
                project_dir="/tmp",
                max_retries=2,
            )


# ---------------------------------------------------------------------------
# Prompt loader + factory
# ---------------------------------------------------------------------------


class TestPromptAndFactory:
    def test_prompt_template_loads_with_version_marker(self):
        text = load_prompt_template("v1")
        assert "version: v1" in text
        assert "MigrationProposal" in text

    def test_make_llm_defaults_to_mock(self, monkeypatch):
        monkeypatch.delenv("DRIFT_LLM_PROVIDER", raising=False)
        llm = make_llm()
        assert llm.name == "mock"

    def test_make_llm_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            make_llm("nope")

    def test_make_llm_anthropic_raises_on_missing_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        llm = make_llm("anthropic")
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY environment variable is required"):
            llm.draft("p")

    def test_make_llm_openai_raises_on_missing_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        llm = make_llm("openai")
        with pytest.raises(ValueError, match="OPENAI_API_KEY environment variable is required"):
            llm.draft("p")

    def test_anthropic_draft_success(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

        class MockResponse:
            def json(self):
                return {
                    "content": [{"text": '{"summary": "claudey"}'}],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                    },
                }

            def raise_for_status(self):
                pass

        posted_args = []

        def mock_post(url, headers, json, timeout):
            posted_args.append((url, headers, json, timeout))
            return MockResponse()

        monkeypatch.setattr(httpx, "post", mock_post)

        llm = make_llm("anthropic")
        res = llm.draft("hello")

        assert res.content == '{"summary": "claudey"}'
        assert res.tokens_in == 100
        assert res.tokens_out == 200
        assert res.cost_usd == pytest.approx(0.0033)
        assert len(posted_args) == 1
        url, headers, payload, timeout = posted_args[0]
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "fake-key"
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        assert timeout == 30.0

    def test_openai_draft_success(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")

        class MockResponse:
            def json(self):
                return {
                    "choices": [{"message": {"content": '{"summary": "openaio"}'}}],
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 150,
                    },
                }

            def raise_for_status(self):
                pass

        posted_args = []

        def mock_post(url, headers, json, timeout):
            posted_args.append((url, headers, json, timeout))
            return MockResponse()

        monkeypatch.setattr(httpx, "post", mock_post)

        llm = make_llm("openai")
        res = llm.draft("hello-openai")

        assert res.content == '{"summary": "openaio"}'
        assert res.tokens_in == 50
        assert res.tokens_out == 150
        assert res.cost_usd == pytest.approx(0.0000975)
        assert len(posted_args) == 1
        url, headers, payload, timeout = posted_args[0]
        assert url == "https://api.openai.com/v1/chat/completions"
        assert headers["Authorization"] == "Bearer fake-openai-key"
        assert payload["messages"] == [{"role": "user", "content": "hello-openai"}]
        assert timeout == 30.0


# ---------------------------------------------------------------------------
# DraftOutcome dataclass smoke
# ---------------------------------------------------------------------------


def test_draft_outcome_dataclass_holds_everything():
    out = DraftOutcome(
        proposal=None,
        response=LLMResponse(content="", cost_usd=0.0),
        attempts=1,
        total_cost_usd=0.0,
        errors=("err",),
    )
    assert out.errors == ("err",)
