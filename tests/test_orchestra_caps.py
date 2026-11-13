"""第1弾(2026-10-28): orchestra の役割別生成上限と思考オフの配線を検証する。

ベースライン実測で生成の約2/3が中間役割の思考だったため、planner と
サブエージェントは think=False + 上限、最終回答を書く aggregator は
思考を残して上限だけ緩く付ける。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestrator
from orchestrator import Orchestrator


class FakeBus:
    def __init__(self):
        self.nodes = {}
        self._seq = 0

    def create_node(self, kind, title, detail, parent_id):
        self._seq += 1
        node_id = f"n{self._seq}"
        self.nodes[node_id] = {"kind": kind, "title": title}
        return node_id

    def complete(self, node_id, output=None, error=None):
        self.nodes[node_id]["output"] = output
        self.nodes[node_id]["error"] = error


def _orchestrator_with_recorder(monkeypatch, reply):
    """_stream_llm を記録スタブに差し替えた Orchestrator と記録リストを返す。"""
    calls = []

    async def fake_stream(bus, node_id, messages, cfg, key, *args, **kwargs):
        calls.append(kwargs)
        return reply

    monkeypatch.setattr(orchestrator, "_stream_llm", fake_stream)
    return Orchestrator(FakeBus(), cfg={}, model="deepseek_strata"), calls


def test_plan_disables_thinking_and_caps_generation(monkeypatch):
    plan_json = ('{"creates_code": false, "subtasks": ['
                 '{"title": "t1", "instruction": "i1"}]}')
    orc, calls = _orchestrator_with_recorder(monkeypatch, plan_json)
    subtasks, creates_code = asyncio.run(orc._plan("task", "root"))
    assert len(subtasks) == 1 and creates_code is False
    assert calls[0]["think"] is False
    assert calls[0]["num_predict"] == orchestrator._PLAN_NUM_PREDICT


def test_subagent_disables_thinking_and_caps_generation(monkeypatch):
    orc, calls = _orchestrator_with_recorder(monkeypatch, "result-body")
    title, result = asyncio.run(
        orc._run_subagent("task", {"title": "t", "instruction": "i"}, "root", False))
    assert (title, result) == ("t", "result-body")
    assert calls[0]["think"] is False
    # 非コードは証拠カード形式のため上限はカード用 (2026-10-28 追加)
    assert calls[0]["num_predict"] == orchestrator._CARD_NUM_PREDICT


def test_subagent_code_keeps_prose_cap(monkeypatch):
    orc, calls = _orchestrator_with_recorder(monkeypatch, "result-body")
    asyncio.run(
        orc._run_subagent("task", {"title": "t", "instruction": "i"}, "root", True))
    assert calls[0]["num_predict"] == orchestrator._SUBAGENT_NUM_PREDICT


def test_aggregator_keeps_thinking_with_loose_cap(monkeypatch):
    orc, calls = _orchestrator_with_recorder(monkeypatch, "final-answer")
    final = asyncio.run(orc._aggregate("task", [("t", "body")], "root", False))
    assert final == "final-answer"
    # 最終回答の質を落とさないため思考は切らない(think は未指定)
    assert calls[0].get("think") is None
    assert calls[0]["num_predict"] == orchestrator._AGGREGATE_NUM_PREDICT


def test_plan_uses_planner_model_when_set(monkeypatch):
    plan_json = ('{"creates_code": false, "subtasks": ['
                 '{"title": "t1", "instruction": "i1"}]}')
    keys = []

    async def fake_stream(bus, node_id, messages, cfg, key, *args, **kwargs):
        keys.append(key)
        return plan_json

    monkeypatch.setattr(orchestrator, "_stream_llm", fake_stream)
    orc = Orchestrator(FakeBus(), cfg={}, model="deepseek_strata",
                       planner_model="distill4b")
    asyncio.run(orc._plan("task", "root"))
    assert keys == ["distill4b"]

    keys.clear()
    orc2 = Orchestrator(FakeBus(), cfg={}, model="deepseek_strata")
    asyncio.run(orc2._plan("task", "root"))
    assert keys == ["deepseek_strata"]  # 未指定なら実行モデルが分解する


def test_caps_do_not_cut_baseline_answer():
    # ベースライン実測(run 331c299999)の aggregator 生成は 2,671 tok。
    # 上限がこれ以下だと通常の最終回答を切ってしまう
    assert orchestrator._AGGREGATE_NUM_PREDICT > 2671
    # サブの本文(最大1,861字 ≒ 1.3K tok 程度)は上限内であること
    assert orchestrator._SUBAGENT_NUM_PREDICT >= 1300
