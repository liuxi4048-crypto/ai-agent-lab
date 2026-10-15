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
elif mode == "incomplete":
    sys.stdin.buffer.read()
    print("レビュー未完了。ファイルを読めなかった", flush=True)
    print("REVIEW_STATUS: incomplete", flush=True)
elif mode == "complete-with-marker":
    sys.stdin.buffer.read()
    # レビュー対象のコードが識別子を含む場合(panel.py 自身のレビューなど)
    print("### helper_unknown_error を握りつぶしている", flush=True)
    print("- 重大度: medium", flush=True)
    print("REVIEW_STATUS: complete", flush=True)
    # codex が読んだファイルの中身はログ行ではないので数えない
    print('CODEX_RUNTIME_ERRORS = ("helper_unknown_error",)', file=sys.stderr, flush=True)
elif mode == "complete-with-tool-errors":
    sys.stdin.buffer.read()
    print("指摘なし", flush=True)
    print("REVIEW_STATUS: complete", flush=True)
    for _ in range(2):
        print("2026-10-15T02:48:03.812355Z ERROR codex_core::tools::router: error=exec_command failed: "
              'CreateProcess { message: "Rejected(\\"Failed to create unified exec process: '
              'helper_unknown_error: setup refresh had errors\\")" }', file=sys.stderr, flush=True)
elif mode == "runtime-error":
    sys.stdin.buffer.read()
    # 2026-10-15 に観測した形: exit 0 のまま、読めなかったことを本文で報告する(申告行なし)
    print("レビュー未完了。実行環境が `helper_unknown_error: setup refresh had errors` で起動できなかった。", flush=True)
    print("2026-10-15T02:48:03.812355Z ERROR codex_core::tools::router: error=exec_command failed: "
          "Failed to create unified exec process: helper_unknown_error: setup refresh had errors",
          file=sys.stderr, flush=True)
elif mode == "echo-prompt":
    with open(os.environ["FAKE_CODEX_PROMPT"], "wb") as f:
        f.write(sys.stdin.buffer.read())
    print("指摘なし", flush=True)
    print("REVIEW_STATUS: complete", flush=True)
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


# ---------------------------------------------------------------- レビューの完了申告

def test_incomplete_status_is_a_failure(fake, tmp_path):
    fake.mode("incomplete")
    res = run_lens(tmp_path)
    assert res["error"] and "REVIEW_STATUS: incomplete" in res["error"]
    assert "ファイルを読めなかった" in res["error"]   # 失敗時も何を書いたかが分かる
    assert "REVIEW_STATUS" not in res["output"]


def test_complete_status_wins_over_identifiers_in_the_review(fake, tmp_path):
    fake.mode("complete-with-marker")
    res = run_lens(tmp_path)
    assert res["error"] is None
    assert res["output"] == "### helper_unknown_error を握りつぶしている\n- 重大度: medium"   # 注記も付かない


def test_tool_failures_in_codex_log_are_noted_on_a_complete_review(fake, tmp_path):
    fake.mode("complete-with-tool-errors")
    res = run_lens(tmp_path)
    assert res["error"] is None   # 本文だけで判断できたと申告しているので成功のまま
    assert res["output"].startswith("指摘なし\n\n> [panel] codex のツール実行が2回失敗(")
    assert "helper_unknown_error" in res["output"]
    assert "提示した本文だけでレビューした可能性がある" in res["output"]


def test_runtime_error_reported_without_status_is_a_failure(fake, tmp_path):
    fake.mode("runtime-error")
    res = run_lens(tmp_path)
    assert res["error"] and "実行基盤が失敗" in res["error"]
    assert "helper_unknown_error" in res["error"]
    assert "setup refresh had errors" in res["error"]


def test_codex_prompt_asks_for_the_status_line(fake, tmp_path, monkeypatch):
    prompt_file = tmp_path / "prompt.txt"
    monkeypatch.setenv("FAKE_CODEX_PROMPT", str(prompt_file))
    fake.mode("echo-prompt")
    res = run_lens(tmp_path)
    assert res["error"] is None and res["output"] == "指摘なし"
    prompt = prompt_file.read_text(encoding="utf-8")
    assert prompt.rstrip().endswith(panel.CODEX_STATUS_INSTRUCTION.strip())
    assert "REVIEW_STATUS: complete" in prompt and "REVIEW_STATUS: incomplete" in prompt
    # 追加ファイルを読めないことだけでは未完了にしない(提示本文で足りるレビューを失敗にしない)
    assert "それだけでは incomplete にしない" in prompt


def test_failed_lens_is_counted_in_the_summary(fake, tmp_path):
    fake.mode("runtime-error")
    broken = run_lens(tmp_path)
    fake.mode("ok")
    fine = run_lens(tmp_path)
    md = panel.render_markdown([broken, {**fine, "lens": "security"}], "t.py", 1.0)
    assert "成功 1 / 失敗 1" in md
    assert "**失敗**: codex の実行基盤が失敗" in md


def test_codex_tool_failures_reads_only_codex_log_lines():
    log = ("2026-10-15T02:48:03.812355Z  INFO codex_core: starting\n"
           "2026-10-15T02:48:03.812355Z ERROR codex_core::tools::router: error=exec_command failed: "
           "helper_unknown_error\n"
           "    helper_unknown_error in a file the agent printed\n"
           "ERROR codex_core::tools::router: no timestamp, not a log line\n")
    assert panel.codex_tool_failures(log) == (1, ["helper_unknown_error"])
    assert panel.codex_tool_failures("") == (0, [])
    assert panel.codex_tool_failures(None) == (0, [])


@pytest.mark.parametrize("output,failed,body", [
    ("指摘なし", False, "指摘なし"),                                     # 申告行なし・識別子なし: 従来どおり成功
    ("指摘なし\nREVIEW_STATUS: complete", False, "指摘なし"),
    ("指摘なし\n  review_status: Complete  ", False, "指摘なし"),       # 大小・前後空白は許す
    ("途中まで\nREVIEW_STATUS: incomplete", True, "途中まで"),
    ("REVIEW_STATUS: complete\n追記\nREVIEW_STATUS: incomplete", True, "追記"),  # 最後の申告が有効
    ("Failed to create unified exec process", True, "Failed to create unified exec process"),
    ("本文中の REVIEW_STATUS: complete という文字列", False, "本文中の REVIEW_STATUS: complete という文字列"),  # 行でなければ申告ではない
    ("途中まで\r\nREVIEW_STATUS: incomplete\r\n", True, "途中まで"),             # CRLF でも読む
    ("REVIEW_STATUS: incomplete\n追記", True, "追記"),                           # 申告の後に文が続いても読む
    # コードブロック内の同じ行は引用であって申告ではない
    ("```\nREVIEW_STATUS: incomplete\n```\n指摘なし\nREVIEW_STATUS: complete", False,
     "```\nREVIEW_STATUS: incomplete\n```\n指摘なし"),
    ("```\nREVIEW_STATUS: complete\n```\nhelper_unknown_error で読めなかった", True,
     "```\nREVIEW_STATUS: complete\n```\nhelper_unknown_error で読めなかった"),
    ("> REVIEW_STATUS: complete\n指摘なし", False, "> REVIEW_STATUS: complete\n指摘なし"),  # 引用行も申告ではない
])
def test_codex_review_problem(output, failed, body):
    problem, text = panel.codex_review_problem(output)
    assert (problem is not None) is failed
    assert text == body

