"""第2弾(2026-10-28): 単発タスクの direct モード振り分けと単一呼び出し実行。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm
import orchestrator
import router
from orchestrator import DirectOrchestrator

BENCH_TASK = "中学生向けに、分数の割り算がなぜ逆数をかけるのかを、例題2つ付きで説明して"


def test_pick_mode_direct_for_simple_explanation():
    assert router.pick_mode(BENCH_TASK) == "direct"
    assert router.pick_mode("なぜ空は青いの？") == "direct"
    assert router.pick_mode("相対性理論とは何か教えて") == "direct"


def test_pick_mode_keeps_orchestra_for_broad_tasks():
    assert router.pick_mode("AとBの違いを比較して説明して") == "orchestra"
    assert router.pick_mode("このテーマについて調査して意見を聞かせて") == "orchestra"
    assert router.pick_mode("次の文章を要約して説明して") == "orchestra"
    # 長文(素材付き)は direct にしない
    long_task = "説明して" + "あ" * 250
    assert router.pick_mode(long_task) == "orchestra"


def test_pick_mode_other_modes_unchanged():
    assert router.pick_mode("ブロック崩しゲームを作って") == "code"
    assert router.pick_mode("この企画書を推敲して") == "critique"


def test_triage_heuristic_returns_direct_without_llm(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("LLM を呼んではいけない")

    monkeypatch.setattr(llm, "chat", boom)
    out = asyncio.run(router.triage({"models": {}}, BENCH_TASK, "x"))
    assert out["mode"] == "direct" and out["by"] == "heuristic"


def test_triage_schema_includes_direct():
    enum = router.TRIAGE_SCHEMA["properties"]["mode"]["enum"]
    assert "direct" in enum


class FakeBus:
    def __init__(self):
        self.nodes = {}
        self._seq = 0
        self.finished = False

    def reset(self):
        pass

    def create_node(self, kind, title, detail, parent_id=None):
        self._seq += 1
        node_id = f"n{self._seq}"
        self.nodes[node_id] = {"kind": kind, "title": title}
        return node_id

    def set_status(self, node_id, status):
        self.nodes[node_id]["status"] = status

    def complete(self, node_id, output=None, error=None):
        self.nodes[node_id]["output"] = output
        self.nodes[node_id]["error"] = error

    def cancel_all(self, reason):
        pass

    def run_finished(self, error=None):
        self.finished = True
        self.error = error


def test_direct_orchestrator_single_call(monkeypatch):
    calls = []

    async def fake_stream(bus, node_id, messages, cfg, key, **kwargs):
        calls.append((node_id, messages, kwargs))
        return "ANS"

    monkeypatch.setattr(orchestrator, "_stream_llm", fake_stream)
    bus = FakeBus()
    orc = DirectOrchestrator(bus, cfg={}, model="deepseek_strata")
    asyncio.run(orc.run("task"))
    assert len(calls) == 1                       # 呼び出しは1回だけ
    assert calls[0][2]["num_predict"] == orchestrator._AGGREGATE_NUM_PREDICT
    answers = [n for n in bus.nodes.values() if n["kind"] == "answer"]
    assert len(answers) == 1 and answers[0]["output"] == "ANS"
    assert bus.finished and bus.error is None
