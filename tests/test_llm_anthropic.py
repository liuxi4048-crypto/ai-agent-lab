import asyncio
import copy
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm

CFG = {"models": {"claude": {"tag": "claude-test", "backend": "anthropic",
       "tools": True, "think": True, "num_ctx": 100000,
       "options": {"temperature": 0.6, "top_p": 0.9, "top_k": 20, "min_p": 0}}}}


def mock_client(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: client(
        transport=httpx.MockTransport(handler), **kw))


def payload(messages, **kwargs):
    return llm._payload(llm.resolve(CFG, "claude"), messages, kwargs.get("tools"),
                        kwargs.get("temperature"), None, kwargs.get("json_mode", False),
                        True, kwargs.get("schema"), None, kwargs.get("num_predict"))


def test_payload():
    messages = [{"role": "system", "content": "first"},
                {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
                {"role": "system", "content": "second"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "one", "arguments": {"q": "日本語"}}},
                    {"id": "two", "function": {"name": "two", "arguments": "{}"}}]},
                {"role": "tool", "content": "ok"},
                {"role": "tool", "tool_call_id": "two", "content": "done"}]
    original = copy.deepcopy(messages)
    tools = [{"type": "function", "function": {"name": "one", "description": "検索",
              "parameters": {"type": "object"}}}]
    result = llm._anthropic_payload(payload(messages, tools=tools, temperature=0.2, num_predict=30))
    assert result["system"] == "first\n\nsecond"
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "user"]
    assert len(result["messages"][0]["content"]) == 2
    calls = result["messages"][1]["content"]
    results = result["messages"][2]["content"]
    assert calls[0]["input"] == {"q": "日本語"}
    assert [b["tool_use_id"] for b in results] == [b["id"] for b in calls]
    assert result["tools"] == [{"name": "one", "description": "検索", "input_schema": {"type": "object"}}]
    assert result["max_tokens"] == 30 and result["temperature"] == 0.2
    assert result["top_k"] == 20 and result["top_p"] == 0.9
    assert not {"think", "thinking", "num_ctx", "min_p", "tool_choice", "options"} & result.keys()
    assert messages == original
    assert llm._anthropic_payload(payload([]))["max_tokens"] == llm.ANTHROPIC_MAX_TOKENS
    assert llm.resolve(CFG, "claude")["base_url"] == llm.ANTHROPIC_BASE


@pytest.mark.parametrize("kwargs", [{"json_mode": True}, {"schema": {"type": "object"}}])
def test_json_warning(kwargs):
    with pytest.warns(UserWarning, match="未対応"):
        result = llm._anthropic_payload(payload([], **kwargs))
    assert "output_config" not in result


def stream_events():
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "考察"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "tool1", "name": "lookup", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"q":'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"}]


def test_stream(monkeypatch):
    def handler(request):
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "test-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=''.join(
            'event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in stream_events()))
    mock_client(monkeypatch, handler)
    notifications = []
    msg = llm.chat_sync(CFG, "claude", [], on_delta=lambda *x: notifications.append(x))
    assert msg["content"] == "hello" and msg["_thinking"] == "考察"
    assert msg["tool_calls"] == [{"id": "tool1", "type": "function", "function": {"name": "lookup", "arguments": {"q": "x"}}}]
    assert msg["_usage"] == 4 and msg["_prompt_tokens"] == 10
    assert msg["_done_reason"] == "tool_calls"
    assert notifications == [("thinking", "考察"), ("content", "hello")]
    timing = msg["_timing"]
    assert set(timing) == set(llm._timing_from_done({}, 0, None))
    assert timing["prompt_tokens"] == 10 and timing["eval_tokens"] == 4
    assert timing["wall_ms"] >= 0 and timing["ttft_ms"] >= 0
    assert timing["eval_ms"] is None
    async def stream():
        return [e async for e in llm.chat_stream(CFG, "claude", [])]
    events = asyncio.run(stream())
    assert events[:2] == [{"thinking": "考察"}, {"content": "hello"}]
    assert events[-1]["meta"]["eval_count"] == 4


def test_nonstream(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={
        "content": [{"type": "text", "text": "ok"}, {"type": "thinking", "thinking": "考"},
                    {"type": "tool_use", "id": "a", "name": "lookup", "input": {"x": 1}}],
        "stop_reason": "max_tokens", "usage": {"input_tokens": 0, "output_tokens": 2}}))
    msg = llm.chat_sync(CFG, "claude", [])
    assert msg["content"] == "ok" and msg["_thinking"] == "考"
    assert msg["tool_calls"][0]["function"]["arguments"] == {"x": 1}
    assert msg["_done_reason"] == "length" and msg["_prompt_tokens"] == 0


def test_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.OllamaFatal, match="ANTHROPIC_API_KEY.*環境変数"):
        llm.chat_sync(CFG, "claude", [])


@pytest.mark.parametrize("status, error", [(401, llm.OllamaFatal), (400, llm.OllamaFatal), (500, llm.OllamaError)])
def test_http_errors(monkeypatch, status, error):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="test-secret")
    mock_client(monkeypatch, handler)
    with pytest.raises(error, match=f"HTTP {status}") as exc:
        llm.chat_sync(CFG, "claude", [], retries=1 if status == 500 else 3)
    assert "test-secret" not in str(exc.value)
    assert len(requests) == 1


def test_incomplete_stream(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, text='data: {"type":"ping"}\n\n'))
    with pytest.raises(llm.OllamaError, match="message_stop"):
        llm.chat_sync(CFG, "claude", [], retries=1)


def test_missing_usage(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={"content": [], "stop_reason": "end_turn"}))
    msg = llm.chat_sync(CFG, "claude", [])
    assert msg["_done_reason"] == "stop"
    assert msg["_timing"]["prompt_tokens"] is None
    assert "_usage" not in msg
