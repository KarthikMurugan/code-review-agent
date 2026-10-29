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
                    "name": "ReviewDraft",
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
    assert payload["tools"][0]["function"]["name"] == "ReviewDraft"
    assert "response_format" not in payload
    assert payload["temperature"] == 1.0


def test_missing_key_fails_before_request(monkeypatch):
    import pytest

    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Set NEBIUS_API_KEY"):
        ModelReviewer(DEFAULT_MODEL, timeout=10)


def test_locations_are_materialized_from_source_not_generated_code(monkeypatch):
    """Replay the observed quoting failure by removing code text from the wire contract."""
    monkeypatch.setenv('NEBIUS_API_KEY', 'synthetic-test-key')
    real_model = langchain_openai.ChatOpenAI
    requests = []
    snapshot = sample_snapshot()
    snapshot.files[0].content = 'def sample():\n    return {"status": "success", "newline": "\\n"}\n'
    draft = {
        'title': 'Zero recipients', 'path': 'app.py', 'start_line': 2, 'end_line': 2,
        'severity': 'medium', 'confidence': 'high', 'explanation': 'Empty input divides by zero.',
        'trigger': 'count=0', 'suggested_fix': 'Reject zero.', 'suggested_test': 'Test zero.',
    }

    def respond(request):
        requests.append(json.loads(request.content))
        schema = requests[-1]['tools'][0]['function']
        # This asserts on the actual HTTP boundary used in the failed live calls.
        assert 'evidence' not in json.dumps(schema['parameters'])
        assert requests[-1]['max_completion_tokens'] <= 4096
        return httpx.Response(200, json={
            'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': 'test',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'call_1', 'type': 'function', 'function': {
                        'name': schema['name'], 'arguments': json.dumps({
                            'findings': [draft, {**draft, 'path': 'not-supplied.py'},
                                         {**draft, 'start_line': 999, 'end_line': 999}],
                            'limitations': [],
                        }),
                    },
                }],
            }}],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(langchain_openai, 'ChatOpenAI', lambda **kwargs: real_model(
                **kwargs, http_async_client=client,
            ))
            return await ModelReviewer(DEFAULT_MODEL, timeout=10).review('bugs', snapshot)

    output = asyncio.run(run())
    assert len(output.findings) == 1
    assert output.findings[0].evidence == snapshot.files[0].content.splitlines()[1]
    assert len(output.limitations) == 2


def test_transient_provider_timeout_is_retried_once(monkeypatch):
    monkeypatch.setenv('NEBIUS_API_KEY', 'synthetic-test-key')
    real_model = langchain_openai.ChatOpenAI
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout('synthetic private payload', request=request)
        name = json.loads(request.content)['tools'][0]['function']['name']
        return httpx.Response(200, json={
            'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': 'test',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'call_1', 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps({'findings': [], 'limitations': []}),
                    },
                }],
            }}],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(langchain_openai, 'ChatOpenAI', lambda **kwargs: real_model(
                **kwargs, http_async_client=client,
            ))
            return await ModelReviewer(DEFAULT_MODEL, timeout=10).review('bugs', sample_snapshot())

    assert asyncio.run(run()) == ReviewOutput()
    assert len(calls) == 2


def test_request_retry_stops_after_two_attempts(monkeypatch):
    import pytest
    monkeypatch.setenv('NEBIUS_API_KEY', 'synthetic-test-key')
    real_model = langchain_openai.ChatOpenAI
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout('PRIVATE', request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(langchain_openai, 'ChatOpenAI', lambda **kwargs: real_model(
                **kwargs, http_async_client=client,
            ))
            return await ModelReviewer(DEFAULT_MODEL, timeout=10).review('bugs', sample_snapshot())

    with pytest.raises(Exception):
        asyncio.run(run())
    assert len(calls) == 2


def test_worker_deadline_settings_fit_queue_lease(monkeypatch):
    import pytest
    from review_agent.config import review_timeout, request_timeout
    monkeypatch.delenv('REVIEW_TIMEOUT_SECONDS', raising=False)
    assert review_timeout() == 480
    assert 2 * request_timeout(review_timeout()) < review_timeout() < 900
    monkeypatch.setenv('REVIEW_TIMEOUT_SECONDS', '600')
    assert review_timeout() == 600
    for value in ['0', '601', 'nan', 'inf', '-1']:
        monkeypatch.setenv('REVIEW_TIMEOUT_SECONDS', value)
        with pytest.raises(ValueError):
            review_timeout()


def test_truncated_output_is_not_reported_as_an_empty_success():
    import pytest
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from langchain_core.messages import AIMessage
    from review_agent.reviewers import IncompleteReviewError

    invoke = AsyncMock(return_value={
        'raw': AIMessage(content='', response_metadata={'finish_reason': 'length'}),
        'parsed': None, 'parsing_error': ValueError('PRIVATE response payload'),
    })
    reviewer = ModelReviewer.__new__(ModelReviewer)
    reviewer.nebius = True
    reviewer.model = SimpleNamespace(with_structured_output=lambda *args, **kwargs:
                                     SimpleNamespace(ainvoke=invoke))
    with pytest.raises(IncompleteReviewError):
        asyncio.run(reviewer.review('bugs', sample_snapshot()))
