"""api_review._files / review の走査まわり(例外の変換、リンクの除外)。"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_review

MARK = "OUTSIDE" + "_MARKER_" + "12345"


def _link_dir(link: Path, target: Path):
    """symlink を試し、権限が無ければ junction。どちらも無理なら skip。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if p.returncode == 0:
            return
    pytest.skip("リンクを作れない環境")


def test_stat_error_does_not_raise(tmp_path, monkeypatch):
    (tmp_path / "ok.txt").write_text("fine", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("nope", encoding="utf-8")
    real = Path.stat

    def fake(self, *a, **k):
        if self.name == "bad.txt":
            raise PermissionError("denied")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "stat", fake)
    out = api_review._files(str(tmp_path))
    assert "fine" in out and "nope" not in out


def test_review_converts_scan_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(api_review, "API_KEY", "k")

    def boom(root):
        raise RuntimeError("walk failed")
    monkeypatch.setattr(api_review, "_files", boom)
    out = asyncio.run(api_review.review(task="t", root=str(tmp_path)))
    assert not out["ok"] and "RuntimeError" in out["error"]


def test_linked_dir_outside_root_excluded(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text(MARK, encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("inside", encoding="utf-8")
    _link_dir(root / "lnk", outside)
    out = api_review._files(str(root))
    assert "inside" in out and MARK not in out


def test_symlinked_file_outside_root_excluded(tmp_path):
    (tmp_path / "o.txt").write_text(MARK, encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("inside", encoding="utf-8")
    try:
        os.symlink(tmp_path / "o.txt", root / "l.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink を作れない環境")
    out = api_review._files(str(root))
    assert "inside" in out and MARK not in out
