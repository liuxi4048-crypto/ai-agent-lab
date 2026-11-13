import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm
import llm_gateway

CFG = {"models": {
    "fast": {"tag": "gemma3:12b", "options": {"temperature": 1.0}},
    "remote": {"tag": "served", "backend": "openai"},
    "claude": {"tag": "claude-x", "backend": "anthropic"},
}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(llm, "load_config", lambda *a, **k: CFG)
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    return TestClient(llm_gateway.app)


def test_tags_lists_only_local_models(client):
    names = [m["name"] for m in client.get("/api/tags").json()["models"]]
    assert names == ["gemma3:12b", "fast"]


def test_generate_passes_through_llm_chat(client, monkeypatch):
    calls = []

    async def fake_chat(cfg, key, messages, **kw):
        calls.append((key, messages, kw))
        return {"role": "assistant", "content": '{"ok": true}'}

    monkeypatch.setattr(llm, "chat", fake_chat)
    r = client.post("/api/generate", json={
        "model": "gemma3:12b", "prompt": "hi", "format": "json",
        "stream": False, "options": {"temperature": 0.2}})
    assert r.status_code == 200
    assert r.json()["response"] == '{"ok": true}'
    key, messages, kw = calls[0]
    assert key == "fast" and messages == [{"role": "user", "content": "hi"}]
    assert kw["temperature"] == 0.2 and kw["json_mode"] is True and kw["json_schema"] is None


def test_generate_passes_schema(client, monkeypatch):
    seen = {}

    async def fake_chat(cfg, key, messages, **kw):
        seen.update(kw)
        return {"content": "{}"}

    monkeypatch.setattr(llm, "chat", fake_chat)
    schema = {"type": "object"}
    client.post("/api/generate", json={"model": "fast", "prompt": "x", "format": schema})
    assert seen["json_schema"] == schema and seen["json_mode"] is False


@pytest.mark.parametrize("model", ["served", "remote", "claude-x", "unknown"])
def test_generate_rejects_non_local_models(client, model):
    r = client.post("/api/generate", json={"model": model, "prompt": "x"})
    assert r.status_code == 404


def test_llm_error_becomes_502(client, monkeypatch):
    async def boom(*a, **k):
        raise llm.OllamaError("down")

    monkeypatch.setattr(llm, "chat", boom)
    r = client.post("/api/generate", json={"model": "fast", "prompt": "x"})
    assert r.status_code == 502 and "down" in r.json()["error"]


@pytest.mark.parametrize("body", ["{broken", "null", "[1]", '{"model": "fast", "prompt": "x", "options": [1]}'])
def test_malformed_body_is_400(client, body):
    r = client.post("/api/generate", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_preload_keeps_explicit_zero_keep_alive(client, monkeypatch):
    import httpx
    sent = {}

    def handler(request):
        sent.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"done": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(llm_gateway.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert client.post("/api/generate", json={"model": "fast", "keep_alive": 0}).status_code == 200
    assert sent == {"model": "gemma3:12b", "keep_alive": 0}


def test_token_required_when_set(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN", "secret-token")
    assert client.get("/api/tags").status_code == 401
    assert client.get("/api/tags", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/tags", headers={"authorization": "Bearer secret-token"}).status_code == 200
