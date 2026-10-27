import asyncio
import copy
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm


CFG = {"models": {"remote": {"tag": "served", "backend": "openai",
       "base_url": "http://localhost:8080/v1/", "tools": True,
       "options": {"temperature": 0.6, "top_k": 20, "min_p": 0}}}}


def mock_client(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: client(
        transport=httpx.MockTransport(handler), **kw))


def sse(chunks):
    return ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'


def test_resolve(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_BASE", raising=False)
    assert llm.resolve(CFG, "remote")["backend"] == "openai"
    assert llm.resolve(CFG, "remote")["base_url"] == "http://localhost:8080/v1/"
    for key in ("old", "unknown"):
        info = llm.resolve({"models": {"old": {"tag": "old"}}}, key)
        assert info["backend"] == "ollama"
        assert info["base_url"] == llm.OLLAMA_BASE
    monkeypatch.setenv("OPENAI_COMPAT_BASE", "http://override/v1")
    cfg = copy.deepcopy(CFG)
    del cfg["models"]["remote"]["base_url"]
    assert llm.resolve(cfg, "remote")["base_url"] == "http://override/v1"


def test_resolve_model_base_url_over_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_BASE", "http://override/v1")
    assert llm.resolve(CFG, "remote")["base_url"] == "http://localhost:8080/v1/"


def test_payload():
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "lookup", "arguments": {"q": "日本語"}}}]},
        {"role": "tool", "tool_name": "lookup", "content": "ok"}]
    original = copy.deepcopy(messages)
    schema = {"type": "object", "properties": {}}
    native = llm._payload(llm.resolve(CFG, "remote"), messages,
        [{"type": "function", "function": {"name": "lookup", "description": "find",
         "parameters": schema}}], 0.2, 100, False, True, schema, True, 30)
    payload = llm._openai_payload(native)
    assert payload["max_tokens"] == 30
    assert payload["temperature"] == 0.2
    assert payload["top_k"] == 20
    assert not {"num_ctx", "num_gpu", "think", "options", "keep_alive"} & payload.keys()
    assert payload["response_format"]["json_schema"]["schema"] == schema
    call = payload["messages"][0]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"q": "日本語"}
    assert payload["messages"][1]["tool_call_id"] == call["id"]
    assert "tool_name" not in payload["messages"][1]
    assert payload["tools"] == native["tools"]
    assert messages == original
    native["format"] = "json"
    assert llm._openai_payload(native)["response_format"] == {"type": "json_object"}


def test_stream_collection(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "考察"}}]},
        {"choices": [{"delta": {"content": "hello", "tool_calls": [
            {"index": 1, "id": "b", "function": {"name": "sec", "arguments": "{"}},
            {"index": 0, "id": "a", "function": {"name": "look", "arguments": '{"q":'}}]}}]},
        {"choices": [{"delta": {"content": "!", "tool_calls": [
            {"index": 0, "function": {"name": "up", "arguments": '"x"}'}},
            {"index": 1, "function": {"name": "ond", "arguments": "}"}}]},
            "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}]
    def handler(request):
        assert str(request.url) == "http://localhost:8080/v1/chat/completions"
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        return httpx.Response(200, text=sse(chunks))
    mock_client(monkeypatch, handler)
    notifications = []
    msg = llm.chat_sync(CFG, "remote", [], on_delta=lambda *x: notifications.append(x))
    assert msg["content"] == "hello!"
    assert msg["_thinking"] == "考察"
    assert msg["tool_calls"][0]["function"] == {"name": "lookup", "arguments": {"q": "x"}}
    assert msg["tool_calls"][1]["function"] == {"name": "second", "arguments": {}}
    assert msg["_usage"] == 4 and msg["_prompt_tokens"] == 10
    assert msg["_done_reason"] == "tool_calls"
    assert msg["_timing"]["eval_ms"] is None
    assert set(msg["_timing"]) == set(llm._timing_from_done({}, 0, None))
    assert notifications == [("thinking", "考察"), ("content", "hello"), ("content", "!")]
    async def stream():
        return [event async for event in llm.chat_stream(CFG, "remote", [])]
    events = asyncio.run(stream())
    assert events[0] == {"thinking": "考察"}
    assert events[-1]["meta"]["eval_count"] == 4
    history = [{"role": "assistant", **msg}]
    llm.strip_meta(history[0])
    history.append({"role": "tool", "tool_name": "lookup", "content": "result"})
    payload = llm._openai_payload(llm._payload(llm.resolve(CFG, "remote"), history,
                                            None, None, None, False, True))
    assert payload["messages"][1]["tool_call_id"] == "a"


@pytest.mark.parametrize("status, error", [(500, llm.OllamaError), (400, llm.OllamaFatal)])
def test_http_errors(monkeypatch, status, error):
    mock_client(monkeypatch, lambda request: httpx.Response(status, text="failure"))
    with pytest.raises(error, match=f"HTTP {status}"):
        llm.chat_sync(CFG, "remote", [], retries=1)


def test_connection_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)
    mock_client(monkeypatch, handler)
    with pytest.raises(llm.OllamaError, match="llama-server / Strata が起動しているか確認") as exc:
        llm.chat_sync(CFG, "remote", [], retries=1)
    assert "http://localhost:8080/v1" in str(exc.value)


def test_missing_usage(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, text=sse([
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])))
    msg = llm.chat_sync(CFG, "remote", [])
    assert msg["_timing"]["prompt_tokens"] is None
    assert msg["_done_reason"] == "stop"


def test_retry_and_fatal(monkeypatch):
    attempts = []
    async def no_delay(*args):
        pass
    monkeypatch.setattr(llm.asyncio, "sleep", no_delay)
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, text=sse([
            {"choices": [{"delta": {"content": "recovered"}, "finish_reason": "stop"}]}]))
    mock_client(monkeypatch, handler)
    assert llm.chat_sync(CFG, "remote", [], retries=2)["content"] == "recovered"
    assert len(attempts) == 2


@pytest.mark.parametrize("status, error", [(500, llm.OllamaError), (404, llm.OllamaFatal)])
def test_stream_http_error(monkeypatch, status, error):
    mock_client(monkeypatch, lambda request: httpx.Response(status, text="failure"))
    async def stream():
        return [event async for event in llm.chat_stream(CFG, "remote", [])]
    with pytest.raises(error):
        asyncio.run(stream())
