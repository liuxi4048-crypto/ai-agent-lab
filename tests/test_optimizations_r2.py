import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm
import runs


# ---- load_config のキャッシュ ----

def test_load_config_cache_and_invalidation(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text("default: a\nmodels: {}\n", encoding="utf-8")
    cfg1 = llm.load_config(str(p))
    assert cfg1["default"] == "a"
    cfg1["default"] = "mutated"                    # 呼び出し側の書き換え
    cfg2 = llm.load_config(str(p))
    assert cfg2["default"] == "a"                   # キャッシュは汚れない
    p.write_text("default: bb\nmodels: {}\n", encoding="utf-8")   # サイズも変わる編集
    cfg3 = llm.load_config(str(p))
    assert cfg3["default"] == "bb"                  # 再起動なしで反映される


# ---- prewarm ----

def _mock_transport(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(handler), **kw))


def test_prewarm_ollama_loads_model(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={})

    _mock_transport(monkeypatch, handler)
    cfg = {"models": {"m": {"tag": "qwen3:30b", "keep_alive": "30m"}}}
    assert asyncio.run(llm.prewarm(cfg, "m")) is True
    assert seen["url"].endswith("/api/generate")
    assert b"qwen3:30b" in seen["body"]


def test_prewarm_skips_openai_and_swallows_errors(monkeypatch):
    called = []
    _mock_transport(monkeypatch, lambda request: called.append(1) or httpx.Response(500))
    cfg = {"models": {"r": {"tag": "served", "backend": "openai",
                            "base_url": "http://localhost:8080/v1"}}}
    assert asyncio.run(llm.prewarm(cfg, "r")) is False
    assert not called                               # 外部バックエンドは対象外

    cfg2 = {"models": {"m": {"tag": "qwen3:30b"}}}
    assert asyncio.run(llm.prewarm(cfg2, "m")) is False   # 500 でも例外にしない


# ---- チェックポイント署名 ----

def test_run_signature_changes_with_state():
    run = runs.Run("task", "code", "coder")
    sig1 = runs._run_signature(run)
    assert runs._run_signature(run) == sig1         # 無変化なら同じ
    node = run.bus.create_node("coder", "作業")
    assert runs._run_signature(run) != sig1         # ノード追加で変わる
    sig2 = runs._run_signature(run)
    run.bus.emit_log(node, "一行")
    assert runs._run_signature(run) != sig2         # ログ追加で変わる


def test_prewarm_default_waits_and_uses_default_key(monkeypatch):
    seen = {}

    async def alive():
        return True

    async def fake_prewarm(cfg, key):
        seen["key"] = key
        return True

    monkeypatch.setattr(llm, "is_alive", alive)
    monkeypatch.setattr(llm, "prewarm", fake_prewarm)
    monkeypatch.setattr(llm, "load_config",
                        lambda path=None: {"default": "coder", "models": {}})
    assert asyncio.run(llm.prewarm_default(wait_sec=1)) is True
    assert seen["key"] == "coder"


def test_prewarm_default_gives_up_without_ollama(monkeypatch):
    async def dead():
        return False

    monkeypatch.setattr(llm, "is_alive", dead)
    assert asyncio.run(llm.prewarm_default(wait_sec=0)) is False
