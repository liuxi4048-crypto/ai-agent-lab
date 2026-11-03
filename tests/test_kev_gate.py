import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import router


def _gate_cfg(**over):
    cfg = {"models": {}, "kev_triage": {"base_url": "http://kev.test"},
           "kev_gate": {"shadow": True}}
    cfg["kev_gate"].update(over)
    return cfg


def _fake_kev_gate(monkeypatch, score=1.6, on_target=0.82):
    captured = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"latency_ms": 123.0, "answers": {
                "fulfillment": {"type": "score", "score": score,
                                "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
                "on_target": {"type": "noul", "noul": on_target}}}

    class Client:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            captured["url"] = url
            captured["body"] = json
            return Resp()

    monkeypatch.setattr(router.httpx, "AsyncClient", Client)
    return captured


def test_gate_config_disabled_by_default():
    assert router.kev_gate_config({}) is None
    # shadow でも base_url が無ければ無効
    assert router.kev_gate_config({"kev_gate": {"shadow": True}}) is None
    cfg = router.kev_gate_config(_gate_cfg())
    assert cfg["base_url"] == "http://kev.test"


def test_gate_judge_returns_verdict(monkeypatch):
    captured = _fake_kev_gate(monkeypatch)
    out = asyncio.run(router.kev_gate_judge(_gate_cfg(), "依頼文", "回答文"))
    assert out["fulfillment_score"] == 1.6
    assert out["on_target_prob"] == 0.82
    assert out["truncated"] is False
    assert captured["url"] == "http://kev.test/v1/systemone"
    assert "依頼文" in captured["body"]["state"]


def test_gate_judge_truncates_long_state(monkeypatch):
    captured = _fake_kev_gate(monkeypatch)
    out = asyncio.run(router.kev_gate_judge(
        _gate_cfg(max_state_chars=100), "依頼", "あ" * 500))
    assert out["truncated"] is True
    assert "中略" in captured["body"]["state"]


def test_gate_judge_none_when_disabled():
    out = asyncio.run(router.kev_gate_judge({"models": {}}, "依頼", "回答"))
    assert out is None
