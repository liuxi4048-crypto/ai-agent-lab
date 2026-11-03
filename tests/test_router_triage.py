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


def _kev_cfg():
    return {"models": {}, "kev_triage": {"base_url": "http://kev.test", "min_prob": 0.5}}


def _fake_kev(monkeypatch, mode, prob, deliverable="html"):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"answers": {
                "mode": {"choice": mode, "probabilities": {mode: prob}},
                "deliverable": {"choice": deliverable, "probabilities": {deliverable: 0.9}}}}

    class Client:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            assert url == "http://kev.test/v1/systemone"
            return Resp()

    monkeypatch.setattr(router.httpx, "AsyncClient", Client)


def test_triage_uses_kev(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("LLM を呼んではいけない")
    monkeypatch.setattr(llm, "chat", boom)
    _fake_kev(monkeypatch, "code", 0.9)
    out = asyncio.run(router.triage(_kev_cfg(), "何か便利なものを作って", "x"))
    assert out == {"mode": "code", "deliverable": "html", "reason": "Kev判定 p=0.90", "by": "kev"}


def test_triage_low_kev_prob_falls_back_to_llm(monkeypatch):
    async def chat(*a, **k):
        return {"content": '{"mode":"orchestra","deliverable":"none","reason":"r"}'}
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "resolve", lambda cfg, key: {"family": "gpt-oss"})
    monkeypatch.setattr(llm, "effort", lambda info, level: level)
    _fake_kev(monkeypatch, "code", 0.3)
    out = asyncio.run(router.triage(_kev_cfg(), "何か便利なものを作って", "x"))
    assert out["by"] == "agent" and out["mode"] == "orchestra" and out["deliverable"] is None
