"""バックエンド別の利用可否判定(model_usable / annotate_models)のテスト。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm


CFG = {"models": {
    "local": {"tag": "qwen:7b"},
    "remote": {"tag": "served", "backend": "openai",
               "base_url": "http://localhost:8080/v1"},
    "cloud": {"tag": "claude-sonnet-5-5", "backend": "anthropic"},
}}


def run(coro):
    return asyncio.run(coro)


def test_ollama_installed():
    assert run(llm.model_usable(CFG, "local", {"qwen:7b"})) == (True, "ok")
    assert run(llm.model_usable(CFG, "local", {"qwen:7b:latest"})) == (True, "ok")
    assert run(llm.model_usable(CFG, "local", set())) == (False, "not_installed")


def test_openai_statuses(monkeypatch):
    async def ok_probe(base):
        return {"served", "other"}

    async def down_probe(base):
        return None

    monkeypatch.setattr(llm, "probe_openai_models", ok_probe)
    assert run(llm.model_usable(CFG, "remote", set())) == (True, "ok")

    async def missing_probe(base):
        return {"other"}

    monkeypatch.setattr(llm, "probe_openai_models", missing_probe)
    assert run(llm.model_usable(CFG, "remote", set())) == (False, "model_missing")

    monkeypatch.setattr(llm, "probe_openai_models", down_probe)
    assert run(llm.model_usable(CFG, "remote", set())) == (False, "server_unreachable")


def test_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert run(llm.model_usable(CFG, "cloud", set())) == (False, "no_api_key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert run(llm.model_usable(CFG, "cloud", set())) == (True, "ok")


def test_annotate_probes_once_per_base(monkeypatch):
    calls = []

    async def counting_probe(base):
        calls.append(base)
        return {"served"}

    monkeypatch.setattr(llm, "probe_openai_models", counting_probe)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = {"models": dict(CFG["models"])}
    cfg["models"]["remote2"] = {"tag": "served", "backend": "openai",
                                "base_url": "http://localhost:8080/v1"}
    models = [llm.resolve(cfg, k) for k in cfg["models"]]
    run(llm.annotate_models(cfg, models, {"qwen:7b"}))
    by_key = {m["key"]: m for m in models}
    assert by_key["local"]["installed"] is True
    assert by_key["remote"]["backend_status"] == "ok"
    assert by_key["remote2"]["backend_status"] == "ok"
    assert by_key["cloud"]["backend_status"] == "no_api_key"
    assert calls == ["http://localhost:8080/v1"]


def test_unusable_reason_mentions_backend():
    info = llm.resolve(CFG, "remote")
    assert "llama-server" in llm.unusable_reason(info, "server_unreachable")
    assert "tag" in llm.unusable_reason(info, "model_missing")
    cloud = llm.resolve(CFG, "cloud")
    assert "ANTHROPIC_API_KEY" in llm.unusable_reason(cloud, "no_api_key")
    local = llm.resolve(CFG, "local")
    assert "ollama pull" in llm.unusable_reason(local, "not_installed")


def test_resolve_server_name_passthrough():
    cfg = {"models": {"s": {"tag": "m1", "backend": "openai",
                            "base_url": "http://localhost:9000/v1",
                            "server_name": "Strata"}}}
    assert llm.resolve(cfg, "s")["server_name"] == "Strata"
    assert llm.resolve(CFG, "remote")["server_name"] == ""
