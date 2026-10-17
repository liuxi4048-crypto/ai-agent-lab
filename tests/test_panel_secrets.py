"""panel.py / api_review.py の秘密情報の外部送信防止のテスト。

ダミーの秘密は実行時に連結して作る(ソースに秘密の形の文字列を直書きしない)。
外部 backend には実際には何も送らない(送信関数は差し替える)。
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel
import api_review

TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"   # github-token(high)
GENERIC = "pass" + "word = " + '"Zq8mT3vK9xLp"'                  # keyvalue(generic のみ)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("PANEL_ALLOW_SECRETS", raising=False)
    monkeypatch.setattr(panel, "_secretscan_mod", None)


def _file(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- ファイル名フィルタ

def test_name_filter_external_vs_local(monkeypatch):
    assert panel.is_secret_path("a/.env")
    assert panel.is_secret_path("x/my_secret.txt")
    assert not panel.is_secret_path("a/.env", external=False)   # ollama のみはフィルタしない
    monkeypatch.setenv("PANEL_ALLOW_SECRETS", "1")
    assert not panel.is_secret_path("a/.env")                    # 解除できるのはこのフィルタだけ


def test_secret_named_file_blocked_for_external_only(tmp_path):
    f = _file(tmp_path, ".env", "A=1\n")
    with pytest.raises(SystemExit) as e:
        panel.collect_content([f], None, 10000, external=True)
    assert "PANEL_ALLOW_SECRETS" in str(e.value)
    body, _ = panel.collect_content([f], None, 10000, external=False)
    assert "A=1" in body


# ---------------------------------------------------------------- 内容検査

def test_high_content_blocked_without_leaking_value(tmp_path):
    f = _file(tmp_path, "app.py", "x = 1\ntoken = '%s'\n" % TOKEN)
    with pytest.raises(SystemExit) as e:
        panel.collect_content([f], None, 10000, external=True)
    msg = str(e.value)
    assert "app.py:2:github-token" in msg
    assert TOKEN not in msg and "local-reviewer" in msg


def test_ollama_only_does_not_scan(tmp_path):
    f = _file(tmp_path, "app.py", "token = '%s'\n" % TOKEN)
    body, _ = panel.collect_content([f], None, 10000, external=False)
    assert TOKEN in body


def test_generic_only_passes(tmp_path):
    f = _file(tmp_path, "app.py", GENERIC + "\n")
    body, _ = panel.collect_content([f], None, 10000, external=True)
    assert "Zq8mT3vK9xLp" in body


def test_allow_secrets_does_not_lift_content_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_ALLOW_SECRETS", "1")
    ok = _file(tmp_path, "secret_notes.txt", "hello\n")
    body, _ = panel.collect_content([ok], None, 10000, external=True)   # 名前フィルタは外れる
    assert "hello" in body
    bad = _file(tmp_path, "config.env", "k=%s\n" % TOKEN)
    with pytest.raises(SystemExit) as e:
        panel.collect_content([bad], None, 10000, external=True)        # 内容検査は外れない
    assert "github-token" in str(e.value) and TOKEN not in str(e.value)


def test_stdin_checked(monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("see %s\n" % TOKEN))
    with pytest.raises(SystemExit) as e:
        panel.collect_content(["-"], None, 10000, external=True)
    assert "stdin:1:github-token" in str(e.value)


def test_secret_beyond_max_chars_still_blocked(tmp_path):
    f = _file(tmp_path, "big.txt", "a" * 500 + "\n%s\n" % TOKEN)
    with pytest.raises(SystemExit):
        panel.collect_content([f], None, 100, external=True)   # 切り詰め後の本文には載らない位置でも止める


def test_secretscan_unavailable_fails_closed(tmp_path, monkeypatch):
    f = _file(tmp_path, "app.py", "x = 1\n")
    monkeypatch.setattr(panel, "SECRETSCAN_PATH", str(tmp_path / "nope.py"))
    with pytest.raises(SystemExit) as e:
        panel.collect_content([f], None, 10000, external=True)
    assert "外部送信しません" in str(e.value)
    # ollama のみなら検査自体が要らないので動く
    body, _ = panel.collect_content([f], None, 10000, external=False)
    assert "x = 1" in body


def test_broken_secretscan_fails_closed(tmp_path, monkeypatch):
    bad = tmp_path / "ss.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(panel, "SECRETSCAN_PATH", str(bad))
    with pytest.raises(SystemExit):
        panel.check_outbound([("a", "x")])


# ---------------------------------------------------------------- main_async の配線

def _args(**kw):
    base = dict(check=False, lens="correctness", preset="default", target=[], diff=None, backend="openrouter",
                or_model=None, model=None, instruction=None, max_chars=10000, format="json")
    base.update(kw)
    return SimpleNamespace(**base)


def test_instruction_secret_blocks_all_external_backends(tmp_path, monkeypatch):
    f = _file(tmp_path, "app.py", "x = 1\n")
    sent = []

    async def fake_or(*a, **k):
        sent.append(1)
        return []
    monkeypatch.setattr(panel, "run_openrouter_all", fake_or)
    monkeypatch.setattr(panel, "openrouter_key", lambda: "k")
    monkeypatch.setattr(panel.llm, "load_config", lambda: {})
    with pytest.raises(SystemExit) as e:
        asyncio.run(panel.main_async(_args(target=[f], instruction="use " + TOKEN)))
    assert "--instruction:1:github-token" in str(e.value) and TOKEN not in str(e.value)
    assert not sent


def test_clean_content_reaches_openrouter(tmp_path, monkeypatch):
    f = _file(tmp_path, "app.py", "x = 1\n")
    sent = []

    async def fake_or(lens_names, content, source, instruction, override, key):
        sent.append(content)
        return [{"lens": "correctness", "backend": "openrouter", "model": "m", "seconds": 0,
                 "output": "ok", "error": None}]
    monkeypatch.setattr(panel, "run_openrouter_all", fake_or)
    monkeypatch.setattr(panel, "openrouter_key", lambda: "k")
    monkeypatch.setattr(panel.llm, "load_config", lambda: {})
    assert asyncio.run(panel.main_async(_args(target=[f]))) == 0
    assert sent and "x = 1" in sent[0]


# ---------------------------------------------------------------- Codex の cwd

def test_make_codex_cwd_copies_only_targets(tmp_path):
    a = _file(tmp_path, "a.py", "A")
    _file(tmp_path, "neighbor.py", "N")
    root = tmp_path / "root"
    root.mkdir()
    cwd = panel.make_codex_cwd(str(root), [a], None)
    assert sorted(os.listdir(cwd)) == ["a.py"]
    empty = panel.make_codex_cwd(str(tmp_path / "root2"), [a], str(tmp_path))
    assert os.listdir(empty) == []


def test_codex_runs_in_temp_cwd_and_cleans_up(tmp_path, monkeypatch):
    sub = tmp_path / "proj"
    sub.mkdir()
    f = _file(sub, "app.py", "x = 1\n")
    _file(sub, "other.py", "y = 2\n")
    seen = {}

    async def fake_codex(lens_names, content, source, instruction, cwd):
        seen["cwd"] = cwd
        seen["files"] = sorted(os.listdir(cwd))
        return [{"lens": "correctness", "backend": "codex", "model": "m", "seconds": 0,
                 "output": "ok", "error": None}]
    monkeypatch.setattr(panel, "run_codex_all", fake_codex)
    monkeypatch.setattr(panel.llm, "load_config", lambda: {})
    assert asyncio.run(panel.main_async(_args(backend="codex", target=[f]))) == 0
    assert seen["files"] == ["app.py"]                     # 隣の other.py は見えない
    assert Path(seen["cwd"]).resolve() != sub.resolve()
    assert not os.path.exists(seen["cwd"])                 # 終了後に削除されている


# ---------------------------------------------------------------- diff のパス解釈

def _sec(header):
    return header + "\n@@ -1 +1 @@\n-a\n+b\n"


def test_diff_paths_with_spaces_and_secret_names():
    diff = (_sec("diff --git a/dir with space/.env b/dir with space/.env")
            + _sec("diff --git a/my notes.txt b/my notes.txt"))
    kept, dropped = panel.strip_secret_diff_sections(diff)
    assert dropped == ["dir with space/.env"]
    assert "my notes.txt" in kept and ".env" not in kept


def test_diff_quoted_japanese_octal_path():
    # "機密 secret.txt" ではなく、日本語ディレクトリ配下の .env(8進エスケープ + 空白)
    jp = "".join("\\%o" % b for b in "設定".encode("utf-8"))
    hdr = 'diff --git "a/%s dir/.env" "b/%s dir/.env"' % (jp, jp)
    kept, dropped = panel.strip_secret_diff_sections(_sec(hdr) + _sec("diff --git a/ok.py b/ok.py"))
    assert dropped == ["設定 dir/.env"]
    assert "ok.py" in kept


def test_diff_rename_to_secret_name_with_spaces():
    sec = ("diff --git a/old name.txt b/new dir/.env\nsimilarity index 90%\n"
           "rename from old name.txt\nrename to new dir/.env\n")
    kept, dropped = panel.strip_secret_diff_sections(sec)
    assert kept == "" and dropped


def test_diff_not_filtered_for_local():
    kept, dropped = panel.strip_secret_diff_sections(_sec("diff --git a/.env b/.env"), external=False)
    assert dropped == [] and ".env" in kept


def test_diff_mode_scans_content(tmp_path):
    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(tmp_path), *a],
                       check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")
    (tmp_path / "a.py").write_text("x = '%s'\n" % TOKEN, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        panel.collect_content([], str(tmp_path), 10000, external=True)
    assert "diff:a.py" in str(e.value) and TOKEN not in str(e.value)
    body, _ = panel.collect_content([], str(tmp_path), 10000, external=False)
    assert TOKEN in body


# ---------------------------------------------------------------- api_review

def test_api_review_blocks_secret(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("k = '%s'\n" % TOKEN, encoding="utf-8")
    monkeypatch.setattr(api_review, "API_KEY", "k")

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("送信してはいけない")
    monkeypatch.setattr(api_review.httpx, "AsyncClient", Boom)
    out = asyncio.run(api_review.review(task="t", root=str(tmp_path)))
    assert not out["ok"]
    assert "github-token" in out["error"] and TOKEN not in out["error"]


def test_api_review_fails_closed_without_secretscan(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(api_review, "API_KEY", "k")
    monkeypatch.setattr(api_review, "SECRETSCAN_PATH", str(tmp_path / "nope.py"))

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("送信してはいけない")
    monkeypatch.setattr(api_review.httpx, "AsyncClient", Boom)
    out = asyncio.run(api_review.review(task="t", root=str(tmp_path)))
    assert not out["ok"] and "外部送信しません" in out["error"]
