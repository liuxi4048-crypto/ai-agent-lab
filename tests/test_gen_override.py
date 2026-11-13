"""生成パラメータ上書き (2026-10-29): OpenAI互換API の temperature / max_tokens を
Run 経由で最終回答の生成まで届ける配線の検証。

- runs: gen_override が Run に保持される
- _stream_llm: gen_override が呼び出し側の役割別既定より優先される
- DirectOrchestrator: override の num_predict / temperature が chat_stream に届く
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestrator
from events import EventBus
from orchestrator import DirectOrchestrator, _stream_llm
from runs import RunManager


def _fake_stream(captured, text="回答テキスト"):
    async def fake(cfg, key, messages, **kwargs):
        captured.update(kwargs)
        yield {"content": text}
        yield {"done": True, "meta": {"eval_count": 7}}
    return fake


def test_run_stores_gen_override():
    mgr = RunManager()
    run = mgr.create("タスク", "direct", "worker",
                     gen_override={"temperature": 0.2, "num_predict": 64})
    assert run.gen_override == {"temperature": 0.2, "num_predict": 64}
    plain = mgr.create("タスク", "direct", "worker")
    assert plain.gen_override == {}


def test_stream_llm_override_wins_over_role_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(orchestrator.llm, "chat_stream", _fake_stream(captured))
    bus = EventBus()
    node = bus.create_node("answer", "最終回答", "")
    out = asyncio.run(_stream_llm(
        bus, node, [{"role": "user", "content": "hi"}], {}, "worker",
        temperature=0.9, num_predict=4000,
        gen_override={"temperature": 0.1, "num_predict": 32}))
    assert out == "回答テキスト"
    assert captured["temperature"] == 0.1
    assert captured["num_predict"] == 32


def test_stream_llm_without_override_keeps_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(orchestrator.llm, "chat_stream", _fake_stream(captured))
    bus = EventBus()
    node = bus.create_node("answer", "最終回答", "")
    asyncio.run(_stream_llm(
        bus, node, [{"role": "user", "content": "hi"}], {}, "worker",
        num_predict=4000))
    assert captured["num_predict"] == 4000
    assert captured["temperature"] is None


def test_direct_orchestrator_forwards_override(monkeypatch):
    captured = {}
    monkeypatch.setattr(orchestrator.llm, "chat_stream", _fake_stream(captured))
    bus = EventBus()
    orch = DirectOrchestrator(bus, {}, "worker",
                              gen_override={"temperature": 0.3, "num_predict": 48})
    asyncio.run(orch.run("短く答えて"))
    assert captured["temperature"] == 0.3
    assert captured["num_predict"] == 48


GOOD = (
    "分数の割り算は、割る数の逆数を掛けて計算します。たとえば 3/4 ÷ 2/5 は "
    "2/5 の逆数 5/2 を掛けて 15/8 になります。\n\n"
    "例題1: 1/2 ÷ 1/4 は、1/4 の逆数 4 を掛けて 2 になります。\n"
    "例題2: 2/3 ÷ 4/9 は、4/9 の逆数 9/4 を掛けて 3/2 になります。\n\n"
    "理由は、割り算が「いくつ分あるか」を数える操作だからです。"
)


def test_stream_llm_think_override_applies_when_unset(monkeypatch):
    # GUI / API の think 指定: 呼び出し側が think を明示していない生成に届く
    captured = {}
    monkeypatch.setattr(orchestrator.llm, "chat_stream", _fake_stream(captured))
    bus = EventBus()
    node = bus.create_node("answer", "最終回答", "")
    asyncio.run(_stream_llm(
        bus, node, [{"role": "user", "content": "hi"}], {}, "worker",
        gen_override={"think": False}))
    assert captured["think"] is False


def test_stream_llm_explicit_think_wins_over_override(monkeypatch):
    # cascade の下書き・骨格など役割上 think=False が決まっている呼び出しは、
    # Run 単位の think 指定 (True) があってもそちらを守る
    captured = {}
    monkeypatch.setattr(orchestrator.llm, "chat_stream", _fake_stream(captured))
    bus = EventBus()
    node = bus.create_node("draft", "下書き", "")
    asyncio.run(_stream_llm(
        bus, node, [{"role": "user", "content": "hi"}], {}, "worker",
        think=False, gen_override={"think": True}))
    assert captured["think"] is False


def test_direct_orchestrator_forwards_think(monkeypatch):
    captured = {}
    monkeypatch.setattr(orchestrator.llm, "chat_stream", _fake_stream(captured))
    bus = EventBus()
    orch = DirectOrchestrator(bus, {}, "worker", gen_override={"think": False})
    asyncio.run(orch.run("短く答えて"))
    assert captured["think"] is False


def test_cascade_draft_also_gets_override(monkeypatch):
    # 下書きがゲートを通過するとそれが最終回答になるため、下書き生成にも
    # override が届くこと (API の max_tokens がカスケード経由でも効く担保)
    from orchestrator import CascadeOrchestrator
    captured = {}
    monkeypatch.setattr(orchestrator.llm, "chat_stream",
                        _fake_stream(captured, text=GOOD))
    bus = EventBus()
    orch = CascadeOrchestrator(bus, {}, "worker", "fast",
                               gen_override={"num_predict": 48})
    asyncio.run(orch.run("中学生向けに、分数の割り算がなぜ逆数をかけるのかを、例題2つ付きで説明して"))
    assert captured["num_predict"] == 48
