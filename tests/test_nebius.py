import asyncio
import json

import httpx
import langchain_openai

from review_agent.config import DEFAULT_MODEL, NEBIUS_BASE_URL, load_environment
from review_agent.demo import sample_snapshot
from review_agent.models import ReviewOutput
from review_agent.reviewers import ModelReviewer


def test_environment_loading_preserves_shell_values_and_literal_secrets(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NEBIUS_API_KEY=literal-${DO_NOT_EXPAND}\nREVIEW_MODEL=file-model\n")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("REVIEW_MODEL", "shell-model")
    # Register restoration for the new variable loaded by dotenv.
    monkeypatch.setenv("NEBIUS_API_KEY", "")
    monkeypatch.delenv("NEBIUS_API_KEY")
    load_environment(env_file)
    import os
    assert os.environ["NEBIUS_API_KEY"] == "literal-${DO_NOT_EXPAND}"
    assert os.environ["REVIEW_MODEL"] == "shell-model"


def test_nebius_request_and_structured_response_without_network(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("NEBIUS_BASE_URL", raising=False)
    real_model = langchain_openai.ChatOpenAI
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "offline-test", "object": "chat.completion", "created": 0,
            "model": "moonshotai/Kimi-K2.7-Code",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "ReviewOutput",
                    "arguments": json.dumps({"findings": [], "limitations": []}),
                }}],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kwargs: real_model(
                **kwargs, http_async_client=client,
            ))
            reviewer = ModelReviewer(DEFAULT_MODEL, timeout=10)
            return await reviewer.review("bugs", sample_snapshot())

    result = asyncio.run(run())
    assert result == ReviewOutput()
    assert len(requests) == 1
    assert str(requests[0].url) == NEBIUS_BASE_URL + "/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer synthetic-test-key"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "moonshotai/Kimi-K2.7-Code"
    assert payload["tools"][0]["function"]["name"] == "ReviewOutput"
    assert "response_format" not in payload
    assert payload["temperature"] == 1.0


def test_missing_key_fails_before_request(monkeypatch):
    import pytest

    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Set NEBIUS_API_KEY"):
        ModelReviewer(DEFAULT_MODEL, timeout=10)
