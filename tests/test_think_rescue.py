"""思考暴走の救済型自動発動 (2026-11-01) の検証。

根拠: think-large-design-20261031/report-thinking-budget-variants.md の
最終判断 (通常は単発 think、思考が閾値を超えた引きだけ _stream_llm 内で
中断して第2段へ切り替える救済型)。ここでは監視の累積カウント・閾値
超過での中断例外・閾値未満での無介入・第2段への引き継ぎ内容を、
LLM ストリームを fake に差し替えて検証する。
"""
import asyncio

import pytest

import orchestrator
from orchestrator import (
    DirectOrchestrator, _ThinkRescueAborted, _estimate_think_tokens,
    _stream_llm, _think_rescue_threshold)


class FakeBus:
    def __init__(self):
        self.logs = []
        self.think = ""
        self.out_text = ""

    def set_prompt(self, node_id, prompt):
        pass

    def set_model(self, node_id, key, tag):
        pass

    def set_status(self, node_id, status):
        pass

    def think_progress(self, node_id, piece):
        self.think += piece

    def token_progress(self, node_id, piece):
        self.out_text += piece

    def set_tokens(self, node_id, n):
        pass

    def emit_log(self, node_id, line):
        self.logs.append(line)


def make_cfg(**extra):
    cfg = {"models": {"m1": {"tag": "fake:1", "think": True, "num_ctx": 16384}}}
    cfg.update(extra)
    return cfg


def call_stream(monkeypatch, chunks, cfg=None, **kwargs):
    async def fake_chat_stream(cfg_, key, messages, **kw):
        for c in chunks:
            yield c

    monkeypatch.setattr(orchestrator.llm, "chat_stream", fake_chat_stream)
    bus = FakeBus()
    result = asyncio.run(_stream_llm(
        bus, "n1", [{"role": "user", "content": "タスク"}],
        cfg if cfg is not None else make_cfg(), "m1", **kwargs))
    return result, bus


# ---- 概算トークン換算 ----

def test_estimate_think_tokens():
    assert _estimate_think_tokens(0) == 0
    assert _estimate_think_tokens(1) == 1
    assert _estimate_think_tokens(3) == 1
    assert _estimate_think_tokens(4) == 2
    # 既定閾値 3072 の発動点は 9216 字超
    assert _estimate_think_tokens(9216) == 3072
    assert _estimate_think_tokens(9217) == 3073


# ---- 閾値の解決 ----

def test_threshold_default():
    assert _think_rescue_threshold({}, "m1") == 3072
    assert _think_rescue_threshold(make_cfg(), "m1") == 3072


def test_threshold_global_override_and_disable():
    assert _think_rescue_threshold(
        make_cfg(think_rescue={"tokens": 2000}), "m1") == 2000
    assert _think_rescue_threshold(
        make_cfg(think_rescue=False), "m1") is None
    assert _think_rescue_threshold(
        make_cfg(think_rescue={"enabled": False}), "m1") is None


def test_threshold_per_model_overrides_global():
    cfg = make_cfg(think_rescue={"tokens": 2000})
    cfg["models"]["m1"]["think_rescue_tokens"] = 1500
    assert _think_rescue_threshold(cfg, "m1") == 1500
    # 0 はそのモデルだけ無効化
    cfg["models"]["m1"]["think_rescue_tokens"] = 0
    assert _think_rescue_threshold(cfg, "m1") is None


# ---- ストリーム監視 ----

def test_monitor_trips_over_threshold(monkeypatch):
    cfg = make_cfg(think_rescue={"tokens": 10})  # 発動点は 31 字超
    chunks = [{"thinking": "あ" * 20}, {"thinking": "あ" * 20},
              {"content": "本文"}, {"done": True, "meta": {}}]
    with pytest.raises(_ThinkRescueAborted) as ei:
        call_stream(monkeypatch, chunks, cfg=cfg, think_rescue=True)
    # 中断時点までの思考全文と概算・閾値が例外に載る
    assert ei.value.thinking == "あ" * 40
    assert ei.value.est_tokens == 14  # ceil(40/3)
    assert ei.value.threshold == 10


def test_monitor_quiet_at_and_below_threshold(monkeypatch):
    cfg = make_cfg(think_rescue={"tokens": 10})
    # 合計 30 字 = 概算 10 トークンちょうど。「超えたら」なので発動しない
    chunks = [{"thinking": "あ" * 20}, {"thinking": "あ" * 10},
              {"content": "本文"}, {"done": True, "meta": {"eval_count": 5}}]
    result, bus = call_stream(monkeypatch, chunks, cfg=cfg, think_rescue=True)
    assert result == "本文"
    assert bus.think == "あ" * 30


def test_monitor_disarmed_when_think_off(monkeypatch):
    cfg = make_cfg(think_rescue={"tokens": 10})
    chunks = [{"thinking": "あ" * 500}, {"content": "本文"},
              {"done": True, "meta": {}}]
    # 呼び出し側が think=False を明示したら監視は武装しない
    result, _ = call_stream(monkeypatch, chunks, cfg=cfg,
                            think=False, think_rescue=True)
    assert result == "本文"
    # モデル既定が判断不能 (think 未指定) でも武装しない
    cfg2 = {"models": {"m1": {"tag": "fake:1", "num_ctx": 16384}},
            "think_rescue": {"tokens": 10}}
    result, _ = call_stream(monkeypatch, chunks, cfg=cfg2, think_rescue=True)
    assert result == "本文"


def test_monitor_disarmed_after_content_starts(monkeypatch):
    cfg = make_cfg(think_rescue={"tokens": 1})
    # 本文が始まった後の thinking チャンクでは中断しない (部分本文を守る)
    chunks = [{"content": "先"}, {"thinking": "あ" * 100},
              {"done": True, "meta": {}}]
    result, _ = call_stream(monkeypatch, chunks, cfg=cfg, think_rescue=True)
    assert result == "先"


def test_monitor_not_armed_without_optin(monkeypatch):
    # think_rescue を渡さない呼び出し (orchestra 等) は従来どおり無介入
    chunks = [{"thinking": "あ" * 10000}, {"content": "本文"},
              {"done": True, "meta": {}}]
    result, _ = call_stream(monkeypatch, chunks, cfg=make_cfg())
    assert result == "本文"


# ---- 第2段への引き継ぎ ----

def test_rescue_answer_builds_stage2(monkeypatch):
    captured = {}

    async def fake_stream_llm(bus, node_id, messages, cfg, key, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "完成した本文"

    monkeypatch.setattr(orchestrator, "_stream_llm", fake_stream_llm)
    bus = FakeBus()
    orch = DirectOrchestrator(bus, make_cfg(), "m1")
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "元のタスク"}]
    rescue = _ThinkRescueAborted("考えたこと" * 10, 999, 3072)
    result = asyncio.run(orch._rescue_answer("a1", messages, rescue))
    assert result == "完成した本文"
    # 第2段は think=False、上限は底上げ後の 8192 (num_ctx 16384・短い入力)
    assert captured["kwargs"]["think"] is False
    assert captured["kwargs"]["num_predict"] == 8192
    user = captured["messages"][-1]["content"]
    assert user.startswith("元のタスク")
    assert "【作成メモ】" in user
    assert user.endswith("考えたこと" * 10)
    # 元の messages は書き換えない
    assert messages[-1]["content"] == "元のタスク"


class RunBus(FakeBus):
    def __init__(self):
        super().__init__()
        self.completed = {}
        self.finished = "not-called"

    def reset(self):
        pass

    def create_node(self, kind, title, detail="", parent_id=None):
        return f"node-{kind}"

    def complete(self, node_id, output=None, error=None):
        self.completed[node_id] = output

    def run_finished(self, error=None):
        self.finished = error


def test_direct_run_rescues_runaway_thinking(monkeypatch):
    calls = []

    async def fake_chat_stream(cfg, key, messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # 第1段: 思考が暴走する (5 × 100 字)
            for _ in range(5):
                yield {"thinking": "あ" * 100}
            yield {"done": True, "meta": {}}
        else:
            # 第2段: think なしで本文を返す
            yield {"content": "救済された本文"}
            yield {"done": True, "meta": {"eval_count": 10}}

    monkeypatch.setattr(orchestrator.llm, "chat_stream", fake_chat_stream)
    bus = RunBus()
    cfg = make_cfg(think_rescue={"tokens": 100})  # 発動点は 301 字超
    orch = DirectOrchestrator(bus, cfg, "m1")
    asyncio.run(orch.run("タスク本文"))
    # 救済が Run ログに残り、最終回答は第2段の本文になる
    assert any("思考救済" in line and "中断" in line for line in bus.logs)
    assert bus.completed.get("node-answer") == "救済された本文"
    assert calls[1].get("think") is False
    assert bus.finished is None
