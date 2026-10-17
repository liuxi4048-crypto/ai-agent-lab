"""panel.collect_content: 追跡済み差分が全て除外でも、未追跡・明示指定ファイルは集める。"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("PANEL_ALLOW_SECRETS", raising=False)
    monkeypatch.setattr(panel, "_secretscan_mod", None)


def _repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(tmp_path), *a],
                       check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    git("add", ".env")
    git("commit", "-q", "-m", "init")
    (tmp_path / ".env").write_text("A=2\n", encoding="utf-8")   # 追跡済み差分は .env のみ


def test_untracked_file_still_collected(tmp_path):
    _repo(tmp_path)
    (tmp_path / "new.py").write_text("print('hello-new')\n", encoding="utf-8")
    body, _ = panel.collect_content([], str(tmp_path), 10000, external=True)
    assert "hello-new" in body and "A=2" not in body


def test_explicit_target_still_collected(tmp_path):
    _repo(tmp_path)
    f = tmp_path.parent / (tmp_path.name + "_t.py")
    f.write_text("print('hello-target')\n", encoding="utf-8")
    body, _ = panel.collect_content([str(f)], str(tmp_path), 10000, external=True)
    assert "hello-target" in body and "A=2" not in body


def test_nothing_left_exits(tmp_path):
    _repo(tmp_path)
    with pytest.raises(SystemExit) as e:
        panel.collect_content([], str(tmp_path), 10000, external=True)
    assert "除外" in str(e.value)
