import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tools


def _toolbox(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\ntarget_token = 42\nprint(target_token)\n",
                                   encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("nothing here\ntarget_token again\n", encoding="utf-8")
    skip = tmp_path / "node_modules"
    skip.mkdir()
    (skip / "c.js").write_text("target_token in skipped dir\n", encoding="utf-8")
    monkeypatch.setattr(tools, "WORKSPACE", str(tmp_path))
    return tools.Toolbox(approve=False)


def test_search_python_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_RG", None)
    tb = _toolbox(tmp_path, monkeypatch)
    out = tb.search_files("target_token")
    assert "a.py:2" in out and "sub/b.txt:2" in out
    assert "node_modules" not in out                    # 除外ディレクトリは検索しない
    assert tb.search_files("zzz_no_match").startswith("(一致なし")


def test_search_rg_path_matches_python(tmp_path, monkeypatch):
    if not tools._RG:
        import pytest
        pytest.skip("ripgrep が無い環境")
    tb = _toolbox(tmp_path, monkeypatch)
    out_rg = tb.search_files("target_token")
    monkeypatch.setattr(tools, "_RG", None)
    out_py = tb.search_files("target_token")
    assert sorted(out_rg.splitlines()) == sorted(out_py.splitlines())


def test_search_rg_failure_falls_back(tmp_path, monkeypatch):
    tb = _toolbox(tmp_path, monkeypatch)
    # Python の re では有効だが Rust regex では無効な後読み → rg がエラーになり従来経路へ
    out = tb.search_files(r"(?<=x = )1")
    assert "a.py:1" in out


def test_toolbox_run_search(tmp_path, monkeypatch):
    import asyncio
    tb = _toolbox(tmp_path, monkeypatch)
    out = asyncio.run(tb.run("search_files", {"pattern": "target_token"}))
    assert "a.py:2" in out
