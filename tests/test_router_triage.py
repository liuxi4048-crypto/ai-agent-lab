import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm
import router


def test_heuristic_confident():
    assert router.heuristic_confident("この企画書を推敲して")
    assert router.heuristic_confident("ポモドーロタイマーのexeが欲しい")
    assert router.heuristic_confident("このコードがなぜ動かないのか説明して")
    assert router.heuristic_confident("TODOアプリのWebページを作って")
    # 曖昧なケースは LLM に回す
    assert not router.heuristic_confident("何か便利なものを作って")
    assert not router.heuristic_confident(
        "フロントとAPIとDBのTODOアプリ一式を作って")   # swarm 境界は曖昧


def test_triage_skips_llm_when_confident(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("LLM を呼んではいけない")
    monkeypatch.setattr(llm, "chat", boom)
    out = asyncio.run(router.triage({"models": {}}, "この企画書を推敲して", "x"))
    assert out["mode"] == "critique" and out["by"] == "heuristic"
