"""証拠カード (2026-10-28): 非コードのサブはカード形式で返し、統合はカード前提で読む。"""
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

    def create_node(self, kind, title, detail, parent_id=None):
        self._seq += 1
        node_id = f"n{self._seq}"
        self.nodes[node_id] = {"kind": kind, "title": title}
        return node_id

    def complete(self, node_id, output=None, error=None):
        self.nodes[node_id]["output"] = output
        self.nodes[node_id]["error"] = error

    def emit_log(self, node_id, line):
        pass


def _recorder(monkeypatch, reply="body"):
    seen = []

    async def fake_stream(bus, node_id, messages, cfg, key, *args, **kwargs):
        seen.append(messages)
        return reply

    monkeypatch.setattr(orchestrator, "_stream_llm", fake_stream)
    return Orchestrator(FakeBus(), cfg={}, model="deepseek_strata"), seen


def test_subagent_non_code_gets_card_format(monkeypatch):
    orc, seen = _recorder(monkeypatch)
    asyncio.run(orc._run_subagent(
        "task", {"title": "t", "instruction": "i"}, "root", False))
    system = seen[0][0]["content"]
    assert "証拠カード" in system
    assert "【主張】" in system and "【数値・固有名詞】" in system


def test_subagent_code_gets_code_format_not_cards(monkeypatch):
    orc, seen = _recorder(monkeypatch)
    asyncio.run(orc._run_subagent(
        "task", {"title": "t", "instruction": "i"}, "root", True))
    system = seen[0][0]["content"]
    assert "コードブロック" in system and "証拠カード" not in system


def test_aggregator_non_code_gets_cards_note(monkeypatch):
    orc, seen = _recorder(monkeypatch, reply="final")
    asyncio.run(orc._aggregate("task", [("t", "【主張】x")], "root", False))
    system = seen[0][0]["content"]
    assert "証拠カード" in system and "作らないでください" in system


def test_aggregator_code_keeps_file_rule(monkeypatch):
    orc, seen = _recorder(monkeypatch, reply="final")
    asyncio.run(orc._aggregate("task", [("t", "code")], "root", True))
    system = seen[0][0]["content"]
    assert "ファイル:" in system and "証拠カード" not in system
