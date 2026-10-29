"""再実行キャッシュ (2026-10-28): 完全一致タスクの保存・即返し・対象モード制限。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import answer_cache


def test_key_normalizes_whitespace():
    assert answer_cache.cache_key("a  b\n c", "m", "direct") == \
        answer_cache.cache_key("a b c", "m", "direct")
    assert answer_cache.cache_key("a b", "m", "direct") != \
        answer_cache.cache_key("a b", "m", "orchestra")
    assert answer_cache.cache_key("a b", "m1", "direct") != \
        answer_cache.cache_key("a b", "m2", "direct")


def test_put_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(answer_cache, "CACHE_PATH", tmp_path / "cache.json")
    assert answer_cache.get("task", "m", "direct") is None
    answer_cache.put("task", "m", "direct", "ANSWER", run_id="r1")
    hit = answer_cache.get("task", "m", "direct")
    assert hit["answer"] == "ANSWER" and hit["run_id"] == "r1"
    answer_cache.put("task", "m", "direct", "")  # 空回答は保存しない
    assert answer_cache.get("task", "m", "direct")["answer"] == "ANSWER"


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
        self.nodes[node_id] = {"kind": kind, "output": None}
        return node_id

    def set_status(self, node_id, status):
        pass

    def emit_log(self, node_id, line):
        pass

    def complete(self, node_id, output=None, error=None):
        self.nodes[node_id]["output"] = output

    def run_finished(self, error=None):
        self.finished = True

    def full_snapshot(self):
        return {"nodes": list(self.nodes.values())}


class FakeRun:
    def __init__(self, mode, model="deepseek_strata"):
        self.bus = FakeBus()
        self.mode = mode
        self.model = model
        self.id = "run-x"


def test_wrap_hit_returns_without_running(tmp_path, monkeypatch):
    monkeypatch.setattr(answer_cache, "CACHE_PATH", tmp_path / "cache.json")
    answer_cache.put("task", "deepseek_strata", "cascade", "CACHED", run_id="r0")
    run = FakeRun("cascade")
    ran = False

    async def inner():
        nonlocal ran
        ran = True

    asyncio.run(answer_cache.wrap(run, "task", inner()))
    assert ran is False
    answers = [n for n in run.bus.nodes.values() if n["kind"] == "answer"]
    assert answers[0]["output"] == "CACHED"
    assert run.bus.finished


def test_wrap_miss_runs_and_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(answer_cache, "CACHE_PATH", tmp_path / "cache.json")
    run = FakeRun("direct")

    async def inner():
        nid = run.bus.create_node("answer", "最終回答", "")
        run.bus.complete(nid, "FRESH")

    asyncio.run(answer_cache.wrap(run, "task", inner()))
    hit = answer_cache.get("task", "deepseek_strata", "direct")
    assert hit["answer"] == "FRESH" and hit["run_id"] == "run-x"


def test_wrap_skips_ineligible_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(answer_cache, "CACHE_PATH", tmp_path / "cache.json")
    answer_cache.put("task", "deepseek_strata", "code", "CACHED", run_id="r0")
    run = FakeRun("code")
    ran = False

    async def inner():
        nonlocal ran
        ran = True

    asyncio.run(answer_cache.wrap(run, "task", inner()))
    assert ran is True  # code はキャッシュ対象外なので必ず実行する
