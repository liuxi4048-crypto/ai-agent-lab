"""insight/agentlab.py(Run 記録の解析)のテスト。実データではなく合成の Run を使う。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from insight import agentlab  # noqa: E402


def _node(id, parent, kind, start, end, tokens=100, status="done", log=None, **kw):
    return {"id": id, "parent_id": parent, "kind": kind, "title": kind, "status": status, "tokens": tokens,
            "started_at": start, "finished_at": end, "log": log or [], "prompt": "p" * 10, "output": "o" * 20,
            "think": kw.get("think", ""), "model_tag": kw.get("model_tag", "")}


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(agentlab, "RUNS_DIR", tmp_path)
    return tmp_path


def _write(d, run_id, mode, nodes, t0=1000.0):
    (d / f"{run_id}.json").write_text(json.dumps({
        "id": run_id, "task": "テスト password=hunter2", "mode": mode, "status": "done", "model": "worker",
        "model_tag": "gpt-oss:20b", "created_at": t0, "finished_at": t0 + 30, "tokens": 300,
        "snapshot": {"nodes": nodes}}), encoding="utf-8")


def test_orchestra_parallel_and_edges(runs_dir):
    _write(runs_dir, "abcdef1234", "orchestra", [
        _node("t", None, "task", 1000, 1030, 0),
        _node("p", "t", "planner", 1001, 1005),
        _node("s1", "t", "subagent", 1005, 1015),
        _node("s2", "t", "subagent", 1006, 1012),
        _node("a", "t", "aggregator", 1016, 1020),
    ])
    tr = agentlab.load_run("abcdef1234")
    assert tr["stats"]["max_parallel"] == 2
    assert tr["run"]["duration"] == 30
    assert "password=<REDACTED>" in tr["run"]["task"]
    s1 = next(n for n in tr["nodes"] if n["id"] == "s1")
    assert s1["start"] == 5 and s1["dur"] == 10 and s1["tps"] == 10.0
    assert "子エージェント" in s1["edge"]
    assert s1["model"] == "gpt-oss:20b"  # ノードにモデル名が無い古い記録は Run のモデルで補う


def test_code_log_parsing_and_candidates(runs_dir):
    log = [
        "=== iter 1/12  phase=PLAN ===",
        "  -> edit_file({\"path\": \"a.html\"})",
        "     [編集エラー] old_string が a.html に見つかりません",
        "  -> edit_file({\"path\": \"a.html\"})",
        "     [編集エラー] old_string が a.html に見つかりません",
        "  -> run_command({\"command\": \"x\"})",
        "     exit=0\n--- stdout ---",
        "  [履歴圧縮] コンテキスト節約のため古いメッセージ1件を要約化(実測 12000/16384 tokens)",
        "\n[MAX_ITER 到達] 安全停止。",
    ]
    _write(runs_dir, "0123456789", "code", [_node("t", None, "task", 1000, 1030, 0), _node("c", "t", "coder", 1001, 1029, log=log)])
    tr = agentlab.load_run("0123456789")
    c = next(n for n in tr["nodes"] if n["id"] == "c")
    assert [t["ok"] for t in c["tools"]] == [False, False, True]
    assert c["compactions"] == [{"used": 12000, "limit": 16384}]
    assert c["stop"] == "max_iter" and c["iters"] == 1 and c["max_iter"] == 12
    keys = {x["key"] for x in tr["candidates"]}
    assert {"edit", "maxiter", "ctx"} <= keys


def test_clip_masks_bearer():
    out = agentlab._clip("curl -H 'Authorization: Bearer abc123def' --token=xyz", 200)
    assert "abc123def" not in out and "xyz" not in out


def test_rejects_bad_id(runs_dir):
    with pytest.raises(ValueError):
        agentlab.load_run("../secret")


def test_missing_times_do_not_crash(runs_dir):
    _write(runs_dir, "fedcba9876", "orchestra", [_node("t", None, "task", 1000, None, 0), _node("s", "t", "subagent", 1001, None, status="generating")])
    tr = agentlab.load_run("fedcba9876")
    s = next(n for n in tr["nodes"] if n["id"] == "s")
    assert s["end"] is None and s["dur"] is None and s["tps"] is None
    assert agentlab.list_runs()[0]["id"] == "fedcba9876"
