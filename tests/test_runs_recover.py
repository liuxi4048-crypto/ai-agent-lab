import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runs


def _write(d: Path, name: str, rec: dict):
    (d / f"{name}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_head_finished():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.json"
        _write(Path(td), "a", {"id": "a", "finished_at": 123.0,
                               "snapshot": {"nodes": []}, "history": []})
        assert runs.RunManager._head_finished(p) is True
        _write(Path(td), "a", {"id": "a", "finished_at": None,
                               "snapshot": {"nodes": []}, "history": []})
        assert runs.RunManager._head_finished(p) is False
        _write(Path(td), "a", {"snapshot": {"x": "y" * 40000}})
        assert runs.RunManager._head_finished(p) is None   # 先頭に無ければ不明


def test_recover_marks_only_unfinished(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path)
    _write(tmp_path, "done1", {"id": "done1", "finished_at": 1.0, "status": "done"})
    _write(tmp_path, "lost1", {"id": "lost1", "finished_at": None, "status": "running"})
    mgr = runs.RunManager()
    assert mgr.recover_interrupted() == 1
    done = json.loads((tmp_path / "done1.json").read_text(encoding="utf-8"))
    lost = json.loads((tmp_path / "lost1.json").read_text(encoding="utf-8"))
    assert done["status"] == "done"
    assert lost["status"] == "interrupted" and lost["finished_at"] is not None


def test_list_runs_limit_keeps_live(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path)
    mgr = runs.RunManager()
    for i in range(10):
        _write(tmp_path, f"r{i:02d}", {"id": f"r{i:02d}", "created_at": float(i),
                                      "finished_at": 1.0, "status": "done"})
    out = mgr.list_runs(limit=3)
    assert [r["id"] for r in out] == ["r09", "r08", "r07"]
