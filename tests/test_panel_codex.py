"""panel.py の codex バックエンド(パイプ受けの固まり対策)のテスト。

本物の codex は使わず、孫プロセスを残して終わる/居座る偽 codex を tmp_path に作って走らせる。
孫は親の stdout/stderr ハンドルを継承して握り続ける(MCPサーバと同じ状況)。
"""
import asyncio
import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel

WINDOWS = os.name == "nt"

FAKE_CODEX = r'''
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
mode = os.environ.get("FAKE_CODEX_MODE", "ok")


def spawn_grandchild():
    # stdout/stderr を継承させて握らせる(close_fds=False)。MCPサーバが残る状況の再現
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         stdout=sys.stdout, stderr=sys.stderr, close_fds=False)
    with open(os.environ["FAKE_CODEX_PIDFILE"], "w") as f:
        f.write(str(p.pid))


if mode == "ok":
    sys.stdin.buffer.read()
    spawn_grandchild()
    print("FAKE-REVIEW 指摘なし", flush=True)
    print("banner on stderr", file=sys.stderr, flush=True)
elif mode == "hang":
    print("PARTIAL-OUT", flush=True)
    print("progress-line", file=sys.stderr, flush=True)
    spawn_grandchild()
    time.sleep(120)
elif mode == "lastmsg":
    sys.stdin.buffer.read()
    path = sys.argv[sys.argv.index("--output-last-message") + 1]
    with open(path, "w", encoding="utf-8") as f:
        f.write("FROM-LAST-MESSAGE")
'''


# ---------------------------------------------------------------- 補助

def pid_alive(pid):
    if WINDOWS:
        # os.kill(pid, 0) は Windows では TerminateProcess になるので絶対に使わない
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | QUERY_LIMITED
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102      # WAIT_TIMEOUT = まだ生きている
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def force_kill(pid):
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """偽 codex を用意して PANEL_CODEX_CMD を向ける。終了時に生き残った孫を殺す。"""
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    if WINDOWS:
        shim = tmp_path / "fake-codex.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = tmp_path / "fake-codex"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    if any(c.isspace() for c in str(shim)):
        pytest.skip("テンプレートを空白で分割するため、空白を含むパスでは実行できない")

    # panel が作る一時ディレクトリをここへ集め、後片付けの成否を確かめられるようにする
    workroot = tmp_path / "tmp"
    workroot.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(workroot))

    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setenv("FAKE_CODEX_PIDFILE", str(pidfile))
    monkeypatch.setenv("PANEL_CODEX_CMD", f"{shim} exec --color never -")
    monkeypatch.delenv("PANEL_CODEX_LAST_MESSAGE", raising=False)

    def grandchild_pid():
        return int(pidfile.read_text()) if pidfile.exists() else None

    yield SimpleNamespace(
        mode=lambda m: monkeypatch.setenv("FAKE_CODEX_MODE", m),
        grandchild_pid=grandchild_pid,
        leftover=lambda: [p.name for p in workroot.glob("panel-codex-*")])

    pid = grandchild_pid()
    if pid and pid_alive(pid):
        force_kill(pid)


def run_lens(tmp_path):
    return asyncio.run(panel.run_codex_lens("correctness", "x = 1", "t.py", None, str(tmp_path)))


# ---------------------------------------------------------------- 実プロセス

def test_grandchild_holding_stdout_does_not_hang(fake, tmp_path):
    fake.mode("ok")
    started = time.monotonic()
    res = run_lens(tmp_path)
    assert time.monotonic() - started < 30
    assert "FAKE-REVIEW" in res["output"]
    assert res["error"] is None
    pid = fake.grandchild_pid()
    assert pid is not None
    assert wait_dead(pid), "正常終了後も孫(MCPサーバ相当)が残っている"
    assert fake.leftover() == [], "孫が握っていた一時ファイルを消せずに残した"


def test_timeout_keeps_partial_output_and_kills_tree(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "CODEX_TIMEOUT", 3)
    fake.mode("hang")
    started = time.monotonic()
    res = run_lens(tmp_path)
    assert time.monotonic() - started < 30
    assert "タイムアウト" in res["error"]
    assert "progress-line" in res["error"]
    assert "PARTIAL-OUT" in res["output"]
    pid = fake.grandchild_pid()
    assert pid is not None
    assert wait_dead(pid), "タイムアウト後も孫が残っている"
    assert fake.leftover() == []


@pytest.mark.skipif(not WINDOWS, reason="taskkill への代替は Windows のみ")
def test_timeout_falls_back_to_taskkill_without_job(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "CODEX_TIMEOUT", 3)
    monkeypatch.setattr(panel, "_job_attach", lambda proc: None)   # Job に入れられなかった状況
    fake.mode("hang")
    started = time.monotonic()
    res = run_lens(tmp_path)
    assert time.monotonic() - started < 30
    assert "タイムアウト" in res["error"]
    assert "PARTIAL-OUT" in res["output"]
    pid = fake.grandchild_pid()
    assert pid is not None
    assert wait_dead(pid), "taskkill /T で孫が止まっていない"


def test_last_message_file_fills_empty_stdout(fake, tmp_path):
    if any(c.isspace() for c in tempfile.gettempdir()):
        pytest.skip("一時ディレクトリが空白を含むと cmd /c 経由では --output-last-message を付けない")
    fake.mode("lastmsg")
    res = run_lens(tmp_path)
    assert res["output"] == "FROM-LAST-MESSAGE"
    assert res["error"] is None
    assert fake.leftover() == []


@pytest.mark.skipif(not WINDOWS, reason="Job Object は Windows のみ")
def test_job_object_kills_child_when_handle_closes():
    """Job が実際に作れて KILL_ON_JOB_CLOSE が効くこと(taskkill への黙った降格を検知する)。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        job = panel._job_attach(proc)
        assert job, "Job Object への割り当てに失敗した"
        panel._k32.CloseHandle(job)   # TerminateJobObject を呼ばず、ハンドルを閉じるだけ
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.skipif(not WINDOWS, reason="Job Object は Windows のみ")
def test_kill_tree_terminates_via_job():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        job = panel._job_attach(proc)
        assert job
        panel._kill_tree(proc, job)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------- with_last_message(純粋関数)

def test_last_message_inserted_before_trailing_dash(monkeypatch):
    monkeypatch.delenv("PANEL_CODEX_LAST_MESSAGE", raising=False)
    argv = ["codex", "exec", "--color", "never", "-"]
    assert panel.with_last_message(argv, "x.txt") == [
        "codex", "exec", "--color", "never", "--output-last-message", "x.txt", "-"]


def test_last_message_appended_without_trailing_dash(monkeypatch):
    monkeypatch.delenv("PANEL_CODEX_LAST_MESSAGE", raising=False)
    assert panel.with_last_message(["codex", "exec"], "x.txt") == [
        "codex", "exec", "--output-last-message", "x.txt"]


@pytest.mark.parametrize("existing", [["-o", "y.txt"],
                                      ["--output-last-message", "y.txt"],
                                      ["--output-last-message=y.txt"]])
def test_last_message_not_duplicated(monkeypatch, existing):
    monkeypatch.delenv("PANEL_CODEX_LAST_MESSAGE", raising=False)
    argv = ["codex", "exec", *existing, "-"]
    assert panel.with_last_message(argv, "x.txt") == argv


def test_last_message_not_added_without_exec(monkeypatch):
    monkeypatch.delenv("PANEL_CODEX_LAST_MESSAGE", raising=False)
    argv = ["codex", "login", "-"]
    assert panel.with_last_message(argv, "x.txt") == argv


def test_last_message_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PANEL_CODEX_LAST_MESSAGE", "0")
    argv = ["codex", "exec", "-"]
    assert panel.with_last_message(argv, "x.txt") == argv


def test_last_message_skipped_for_cmd_wrapper_with_space_path(monkeypatch):
    monkeypatch.delenv("PANEL_CODEX_LAST_MESSAGE", raising=False)
    argv = ["cmd", "/c", "C:\\npm\\codex.cmd", "exec", "-"]
    assert panel.with_last_message(argv, "C:\\Users\\A B\\last.txt") == argv


# ---------------------------------------------------------------- without_notify(純粋関数)

def test_notify_disabled_before_trailing_dash():
    assert panel.without_notify(["codex", "exec", "--color", "never", "-"]) == [
        "codex", "exec", "--color", "never", "-c", "notify=[]", "-"]


@pytest.mark.parametrize("existing", [["-c", "notify=['x']"], ["--config=notify=['x']"]])
def test_notify_respects_explicit_setting(existing):
    argv = ["codex", "exec", *existing, "-"]
    assert panel.without_notify(argv) == argv


def test_notify_not_added_without_exec():
    argv = ["codex", "login"]
    assert panel.without_notify(argv) == argv


# ---------------------------------------------------------------- without_hooks(純粋関数)

def test_hooks_disabled_before_trailing_dash():
    assert panel.without_hooks(["codex", "exec", "--color", "never", "-"]) == [
        "codex", "exec", "--color", "never", "-c", "features.hooks=false", "-"]


@pytest.mark.parametrize("existing", [["-c", "features.hooks=true"],
                                      ["--config=features.hooks=true"]])
def test_hooks_respects_explicit_setting(existing):
    argv = ["codex", "exec", *existing, "-"]
    assert panel.without_hooks(argv) == argv


def test_hooks_not_added_without_exec():
    argv = ["codex", "login"]
    assert panel.without_hooks(argv) == argv


def test_review_overrides_stack_before_trailing_dash():
    argv = panel.without_hooks(panel.without_notify(["codex", "exec", "-"]))
    assert argv == ["codex", "exec", "-c", "notify=[]", "-c", "features.hooks=false", "-"]
