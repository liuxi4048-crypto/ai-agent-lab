"""cascade 高速モデルの keep-warm 判定とループのテスト (2026-10-30)。

実測の症状: 高速モデルが VRAM の奪い合いで追い出されると、次の cascade
の下書きが再ロード待ちで最大約120秒止まる。対策は「Ollama が空で、
Run も動いていないときだけ温め直す」。他人のモデルを追い出さない
ことが条件 (奪い合いの連鎖を防ぐ)。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm
from llm import keepwarm_should_load


def test_should_load_only_when_empty_and_idle():
    assert keepwarm_should_load([], "qwen3.8-distill:4b", False) is True
    # 何か載っていたら温めない (そちらを追い出すため)
    assert keepwarm_should_load(["qwen3:30b"], "qwen3.8-distill:4b", False) is False
    assert keepwarm_should_load(["qwen3.8-distill:4b"],
                                "qwen3.8-distill:4b", False) is False
    # Run 実行中は触らない
    assert keepwarm_should_load([], "qwen3.8-distill:4b", True) is False
    # 高速モデル未設定なら何もしない
    assert keepwarm_should_load([], "", False) is False


def test_keepwarm_loop_loads_only_when_empty(monkeypatch):
    calls = []
    state = {"loaded": []}

    async def fake_loaded():
        return state["loaded"]

    async def fake_prewarm(cfg, key):
        calls.append(key)
        state["loaded"] = ["qwen3.8-distill:4b"]
        return True

    monkeypatch.setattr(llm, "loaded_model_tags", fake_loaded)
    monkeypatch.setattr(llm, "prewarm", fake_prewarm)
    monkeypatch.setattr(llm, "load_config",
                        lambda: {"cascade_fast_model": "distill4b",
                                 "models": {"distill4b": {}}})
    monkeypatch.setattr(llm, "resolve",
                        lambda cfg, key: {"backend": "ollama",
                                          "tag": "qwen3.8-distill:4b"})

    async def scenario():
        task = asyncio.create_task(
            llm.keepwarm_fast_model(lambda: 0, interval_sec=0.01))
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    # 空の間に1回だけ温め、載った後は二度と温めない
    assert calls == ["distill4b"]
