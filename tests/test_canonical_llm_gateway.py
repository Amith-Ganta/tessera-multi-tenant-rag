"""Tests for canonical LLM gateway enforcement (Item 6, Hardening 2026-09-24).

Verifies that router, generator, judge, and agent_pipeline all route LLM calls
through llm.complete() instead of calling litellm.completion directly.  Tests
mock llm_complete at the point-of-use in each module so no real API call is made.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Test 1: router._classify_once goes through llm_complete
# ---------------------------------------------------------------------------

def test_router_classify_uses_llm_complete():
    from src.rag import router

    with patch("src.rag.router.llm_complete", return_value=('{"route":"vector","reason":"test"}', {})) as mock_complete:
        result = router._classify_once("What is the refund policy?")

    mock_complete.assert_called_once()
    assert result.get("route") == "vector"


# ---------------------------------------------------------------------------
# Test 2: generator.generate_answer goes through llm_complete
# ---------------------------------------------------------------------------

def test_generator_uses_llm_complete():
    from src.rag import generator

    fake_doc = MagicMock()
    fake_doc.page_content = "Policy text."
    fake_doc.metadata = {"source": "policy.pdf"}

    with patch("src.rag.generator.llm_complete", return_value=("The refund policy is 30 days.", {})) as mock_complete:
        answer = generator.generate_answer("What is the refund policy?", [fake_doc])

    mock_complete.assert_called_once()
    assert answer == "The refund policy is 30 days."


# ---------------------------------------------------------------------------
# Test 3: DeepSeekJudge.generate goes through llm_complete
# ---------------------------------------------------------------------------

def test_judge_generate_uses_llm_complete():
    from src.rag.judge import DeepSeekJudge

    judge = DeepSeekJudge()

    with patch("src.rag.judge.llm_complete", return_value=("judge verdict", {})) as mock_complete:
        result = judge.generate("Is this answer correct?")

    mock_complete.assert_called_once()
    assert result == "judge verdict"


# ---------------------------------------------------------------------------
# Test 4: agent_pipeline._generate goes through llm_complete
# ---------------------------------------------------------------------------

def test_agent_pipeline_generate_uses_llm_complete():
    from src.rag import agent_pipeline

    fake_doc = MagicMock()
    fake_doc.page_content = "Context chunk."

    with patch("src.rag.agent_pipeline.llm_complete", return_value=("Generated answer.", {"prompt": 10, "completion": 5, "total": 15})) as mock_complete:
        content, usage = agent_pipeline._generate("Question?", [fake_doc])

    mock_complete.assert_called_once()
    assert content == "Generated answer."
    assert usage["total"] == 15


# ---------------------------------------------------------------------------
# Test 5: agent_pipeline._self_check goes through llm_complete (json_mode=True)
# ---------------------------------------------------------------------------

def test_agent_pipeline_self_check_uses_llm_complete_json_mode():
    from src.rag import agent_pipeline

    fake_doc = MagicMock()
    fake_doc.page_content = "Context."

    payload = '{"grounded": true, "answers_question": true, "feedback": ""}'

    with patch("src.rag.agent_pipeline.llm_complete", return_value=(payload, {"prompt": 8, "completion": 4, "total": 12})) as mock_complete:
        parsed, usage = agent_pipeline._self_check("Q?", "A.", [fake_doc])

    mock_complete.assert_called_once()
    _, kwargs = mock_complete.call_args
    assert kwargs.get("json_mode") is True, "self_check must request json_mode"
    assert parsed.get("grounded") is True
