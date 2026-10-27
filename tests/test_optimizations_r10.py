"""第10弾の検証: イベント蓄積の list 化、chat_stream リトライ、read_file 範囲読み。"""
import asyncio

import pytest

import events as events_mod
import llm
from tools import Toolbox


# ---- events: 部品バッファ経由でも output/think が正しく組み立つ ----

def test_token_progress_accumulates_via_parts():
    bus = events_mod.EventBus()
    nid = bus.create_node("answer", "回答")
    pieces = [f"piece{i}-" for i in range(50)]
    for p in pieces:
        bus.token_progress(nid, p)
    snap = bus.nodes[nid].snapshot()
    assert snap["output"] == "".join(pieces)
    assert snap["preview"] == "".join(pieces)[-120:]
    assert bus.nodes[nid].output_len() == len("".join(pieces))


def test_think_progress_keeps_tail_cap():
    bus = events_mod.EventBus()
    nid = bus.create_node("coder", "作業")
    big = "x" * 1000
    for _ in range(30):   # 30,000字 > THINK_CAP(20,000)
        bus.think_progress(nid, big)
    snap = bus.nodes[nid].snapshot()
    assert len(snap["think"]) == events_mod.THINK_CAP
    assert bus.nodes[nid].think_len() == events_mod.THINK_CAP


def test_complete_overrides_buffered_output():
    bus = events_mod.EventBus()
    nid = bus.create_node("answer", "回答")
    bus.token_progress(nid, "下書き")
    bus.complete(nid, "最終回答")
    assert bus.nodes[nid].snapshot()["output"] == "最終回答"
    assert bus.nodes[nid].output_len() == len("最終回答")


# ---- llm.chat_stream: 未送信時のみリトライ ----

def _cfg():
    return {"models": {"m": {"tag": "t:1", "backend": "ollama"}}}


def test_chat_stream_retries_before_first_event(monkeypatch):
    calls = {"n": 0}

    async def flaky(info, payload, t):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm.OllamaError("接続直後に切断")
        yield {"content": "ok"}
        yield {"done": True, "meta": {}}

    monkeypatch.setattr(llm, "_chat_stream_once", flaky)
    real_sleep = asyncio.sleep

    async def fast(_s):
        await real_sleep(0)

    monkeypatch.setattr(llm.asyncio, "sleep", fast)

    async def main():
        return [e async for e in llm.chat_stream(_cfg(), "m", [{"role": "user", "content": "x"}])]

    out = asyncio.run(main())
    assert calls["n"] == 2
    assert out[0] == {"content": "ok"}


def test_chat_stream_no_retry_after_first_event(monkeypatch):
    calls = {"n": 0}

    async def broken(info, payload, t):
        calls["n"] += 1
        yield {"content": "途中"}
        raise llm.OllamaError("途中で切断")

    monkeypatch.setattr(llm, "_chat_stream_once", broken)

    async def main():
        return [e async for e in llm.chat_stream(_cfg(), "m", [{"role": "user", "content": "x"}])]

    with pytest.raises(llm.OllamaError):
        asyncio.run(main())
    assert calls["n"] == 1   # 二重出力を避けるため再試行しない


# ---- tools.read_file: 範囲読みが全読みのスライスと一致 ----

def test_read_file_range_matches_slice():
    import os
    import tempfile
    tb = Toolbox()
    d = tempfile.mkdtemp(dir=tb.root)
    try:
        rel = os.path.join(os.path.basename(d), "big.txt")
        with open(os.path.join(tb.root, rel), "w", encoding="utf-8") as f:
            f.write("".join(f"line{i}\n" for i in range(1, 501)))
        ranged = tb.read_file(rel, start_line=100, end_line=110)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    assert "(行 100-110 / 全500行)" in ranged
    for i in range(100, 111):
        assert f"line{i}" in ranged
    assert "line99" not in ranged and "line111" not in ranged
