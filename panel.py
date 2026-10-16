"""外部レビューパネル: ローカルLLM(Ollama)/Codex を「別思考のレビュアー」として並列に走らせるCLI。

Claude Code のサブエージェント(~/.claude/agents/local-reviewer.md, codex-reviewer.md)から
呼ばれることを想定。観点(lens)ごとに独立したコンテキストでレビューさせ、結果を束ねて返す。

VRAM 16GB(RX 9070 XT)前提のスケジューリング:
- モデル単位は**直列**に処理する。異なるモデルを同時に走らせるとVRAMに載りきらず
  ロード/アンロードのスラッシングで逆に遅くなるため(worker 13GB + reasoner 9.3GB = 22GB)。
- 同一モデルに割り当たった複数lensは**並列**に投げる。既にロード済みのモデルへの
  並列リクエストはOllamaが捌けるのでスワップが起きない。
- Codexバックエンドは別プロセス(クラウド実行)なのでVRAMを食わず、Ollama群と同時に走らせる。
- hybrid配置モデルは llm._HYBRID_GATE がプロセス内でさらに直列化する。

使用例:
    python panel.py --target app.py --lens correctness,security
    python panel.py --diff C:\\dev\\myrepo --preset deep --format json
    python panel.py --check
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import llm

# ---------------------------------------------------------------- lens 定義

# model: models.yaml のキー。未インストールなら FALLBACK 順に降格する。
LENSES = {
    "correctness": {
        "model": "reasoner",
        "focus": "正確性。ロジックの誤り、境界条件(空・0・最大値・負値)、off-by-one、"
                 "例外/エラー処理の欠落、null/undefined、競合状態、リソース解放漏れ。",
    },
    "security": {
        "model": "worker",
        "focus": "セキュリティ。入力検証の欠落、インジェクション(SQL/コマンド/パス)、"
                 "認証・認可の抜け、秘密情報のハードコードやログ出力、安全でないデシリアライズ、"
                 "権限昇格につながる既定値。",
    },
    "perf": {
        "model": "worker",
        "focus": "性能。不要なループのネスト、N+1、同期I/Oのブロッキング、"
                 "大きなオブジェクトの無駄なコピー、キャッシュ可能な再計算、計算量の悪化。",
    },
    "design": {
        "model": "smart",
        "focus": "設計。責務の分離、過剰な結合、抽象の漏れ、拡張時に壊れる箇所、"
                 "命名と実体の不一致、既存パターンからの逸脱。",
    },
    "simplify": {
        "model": "coder",
        "focus": "簡素化。重複コード、不要な分岐・変数・抽象、標準ライブラリで置換できる自作処理、"
                 "デッドコード。挙動を変えずに削れる部分だけを挙げる。",
    },
}

PRESETS = {
    "quick": ["correctness"],
    "default": ["correctness", "security", "perf"],
    "deep": ["correctness", "security", "perf", "design", "simplify"],
}

# 希望モデルが未インストールのときの降格順(vram配置=軽いものを優先)
# fast(tier:external=vault専用)と heavy(tier:archive=退役)は降格先から外す(2026-08-22)
FALLBACK = ["worker", "reasoner", "coder", "smart"]

SYSTEM_PROMPT = """あなたはコードレビュアーです。与えられた観点**だけ**に集中し、他の観点は無視してください。

出力は日本語のMarkdown。指摘ごとに次の形式:

### <一行要約>
- 重大度: high | medium | low
- 場所: <ファイル:行 または 関数名>
- 根拠: <該当コードの引用 1〜3行>
- 内容: <何が問題か、どう直すか>

規則:
- 与えられたコードから実際に確認できることだけ書く。推測・一般論・お世辞は書かない。
- 確信が持てない指摘は出さない。誤検出は見逃しより有害。
- 該当する問題が無ければ「指摘なし」とだけ出力する。
- 前置き・要約・締めの文は書かない。指摘だけを出力する。
"""

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"^\s*<think>.*", re.DOTALL | re.IGNORECASE)

# codex CLI の起動コマンド。プロンプトは末尾の `-` により**標準入力**から渡す
# (Windowsのコマンドライン長上限32767を避けるため)。`--sandbox read-only` で
# レビュアーがファイルを書き換えないよう封じる。
# 環境変数 PANEL_CODEX_CMD で上書き可能(codex のCLI仕様変更に追従するため)。
#
# 実行方法の注意(Windows): codex は cmd → node → codex.exe → MCPサーバ群 と孫を生み、孫が
# 標準出力/標準エラーのパイプ書き込みハンドルを握ったまま残る。subprocess.run のパイプ受けは
# 全員が閉じるまで EOF を待つため、codex 本体が終わっても戻らず、タイムアウト後も kill が
# 孫に届かず固まる(取得済みの出力も捨てられる)。そこで stdin/stdout/stderr は一時ファイルに
# 向けて EOF を待たず、プロセスツリーは Job Object(POSIXは新セッション+killpg)で丸ごと止める。
# 詳細は run_process_tree。
# 環境変数: PANEL_CODEX_TIMEOUT=1lensあたりの上限秒(既定900)
#           PANEL_CODEX_LAST_MESSAGE=0 で `--output-last-message` の自動付与を止める。
# config.toml の notify と hooks.json の hook もレビュー実行では `-c notify=[]` /
# `-c features.hooks=false` で止める(without_notify / without_hooks)。
# Windows では elevated サンドボックスの不具合を避けるため unelevated で動かす(with_unelevated_sandbox)。
DEFAULT_CODEX_CMD = "codex exec --skip-git-repo-check --sandbox read-only --color never -"
CODEX_TIMEOUT = float(os.environ.get("PANEL_CODEX_TIMEOUT", "900"))


def strip_think(text):
    """DeepSeek-R1系の <think> ブロックを除去する(閉じタグ欠落時は全体を捨てる)。"""
    text = _THINK_RE.sub("", text)
    return _OPEN_THINK_RE.sub("", text).strip()


def _next_pow2(n):
    """n以上の最小の2の冪を返す。"""
    p = 1
    while p < n:
        p *= 2
    return p


# ---------------------------------------------------------------- 入力の収集

def collect_content(targets, diff_repo, max_chars):
    """レビュー対象のテキストを組み立てる。戻り値: (本文, 由来ラベル)"""
    parts, labels = [], []

    if diff_repo:
        proc = subprocess.run(["git", "-C", diff_repo, "diff", "HEAD"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0:
            raise SystemExit(f"git diff に失敗: {proc.stderr.strip()[:300]}")
        if not proc.stdout.strip():
            raise SystemExit(f"{diff_repo} に未コミットの差分がありません。")
        parts.append(f"--- git diff HEAD ({diff_repo}) ---\n{proc.stdout}")
        labels.append(f"diff:{os.path.basename(os.path.abspath(diff_repo))}")

        # git diff HEAD には未追跡(git add前)の新規ファイルが乗らないため、
        # 別途 ls-files で拾って本文に加える(1ファイルあたり4000字上限)
        untracked = subprocess.run(
            ["git", "-C", diff_repo, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        new_files = [p.strip() for p in (untracked.stdout or "").splitlines() if p.strip()]
        for rel in new_files:
            try:
                with open(os.path.join(diff_repo, rel), "r", encoding="utf-8", errors="replace") as f:
                    text = f.read(4000)
            except OSError:
                continue
            parts.append(f"--- new file: {rel} ---\n{text}")
        if new_files:
            labels.append(f"new:{len(new_files)}件")

    for t in targets:
        if t == "-":
            text = sys.stdin.read()
            parts.append(f"--- stdin ---\n{text}")
            labels.append("stdin")
            continue
        if os.path.isdir(t):
            raise SystemExit(f"ディレクトリは対象にできません(ファイルを指定): {t}")
        with open(t, "r", encoding="utf-8", errors="replace") as f:
            parts.append(f"--- {t} ---\n{f.read()}")
        labels.append(os.path.basename(t))

    if not parts:
        raise SystemExit("--target か --diff のどちらかを指定してください。")

    body = "\n\n".join(parts)
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n...(以降 {len(body) - max_chars} 文字を省略)"
    return body, ", ".join(labels)


def build_prompt(lens_name, focus, content, source, instruction):
    extra = f"\n追加の指示: {instruction}\n" if instruction else ""
    return (
        f"観点: {lens_name}\n"
        f"この観点で見るべき点: {focus}\n"
        f"レビュー対象: {source}\n{extra}\n"
        f"以下をレビューしてください。\n\n{content}\n"
    )


# ---------------------------------------------------------------- バックエンド

async def run_ollama_lens(cfg, model_key, lens_name, content, source, instruction):
    started = time.monotonic()
    info = llm.resolve(cfg, model_key)
    num_ctx = info["num_ctx"]

    prompt = build_prompt(lens_name, LENSES[lens_name]["focus"], content, source, instruction)
    # 概算トークン数(日本語想定でlen//2)がコンテキストの80%を超える場合は
    # num_ctx を「必要量÷0.8」(=SYSTEM_PROMPT・生成分のヘッドルーム込み)まで
    # 32768上限で広げる。上限でも80%予算に収まらない分は対象コード部分の末尾を
    # 切り詰める(境界帯 26K〜32K トークンを無余裕のまま送ると、Ollamaが先頭の
    # system/観点指示から黙って切り捨てるため)。
    req_num_ctx = None
    estimated = (len(prompt) + len(SYSTEM_PROMPT)) // 2
    if estimated > num_ctx * 0.8:
        needed = _next_pow2(int(estimated / 0.8))
        req_num_ctx = min(32768, needed)
        budget_tokens = int(req_num_ctx * 0.8)
        if estimated > budget_tokens:
            overhead_tokens = estimated - (len(content) // 2)   # focus/指示文などcontent以外の分
            keep_chars = max(0, budget_tokens - overhead_tokens) * 2
            if keep_chars < len(content):   # 実際に削れる分がある場合だけ切り詰める
                content = content[:keep_chars] + "\n\n…(入力が長いため末尾を省略)"
                prompt = build_prompt(lens_name, LENSES[lens_name]["focus"], content, source, instruction)

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
    try:
        msg = await llm.chat(cfg, model_key, messages, temperature=0.2, num_ctx=req_num_ctx)
        text = strip_think(msg.get("content", ""))
        if not text:
            # <think>ブロックだけで本文が無い応答。temperatureを上げて1回だけ救済を試みる
            retry = await llm.chat(cfg, model_key, messages, temperature=0.8, num_ctx=req_num_ctx)
            text = strip_think(retry.get("content", ""))
        error = None if text else "思考のみで本文なし"
        return {"lens": lens_name, "backend": "ollama", "model": info["tag"],
                "seconds": round(time.monotonic() - started, 1),
                "output": text or "(空応答)", "error": error}
    except Exception as e:  # llm.OllamaError 含む。1lensの失敗で全体を落とさない
        return {"lens": lens_name, "backend": "ollama", "model": info["tag"],
                "seconds": round(time.monotonic() - started, 1),
                "output": "", "error": f"{type(e).__name__}: {e}"}


if os.name == "nt":
    from ctypes import wintypes

    _JOB_KILL_ON_CLOSE = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _JOB_BASIC_PROCESS_ID_LIST = 3       # JobObjectBasicProcessIdList
    _JOB_EXTENDED_LIMIT_INFO = 9         # JobObjectExtendedLimitInformation
    _SYNCHRONIZE = 0x00100000

    class _JobPidList(ctypes.Structure):
        # JOBOBJECT_BASIC_PROCESS_ID_LIST。codex は MCP サーバを数十個起こすので枠を広めに取る
        _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * 1024)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JobBasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _JobExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JobBasicLimit),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    # HANDLE は64bitなので argtypes/restype を明示する(省略すると int に切り詰められる)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             wintypes.LPVOID, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                               wintypes.DWORD, wintypes.LPDWORD]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    # Popen はスレッドハンドルを閉じてしまい ResumeThread を使えないため、プロセス単位で再開する
    # (psutil の Process.resume() と同じ方法)
    _CREATE_SUSPENDED = 0x4
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long   # NTSTATUS。0 が成功


def _job_attach(proc):
    """Windows: proc を KILL_ON_JOB_CLOSE の Job Object に入れて返す。失敗時は None。

    孫以降は自動で同じ Job に属する。panel.py 自身が落ちてもハンドルが閉じられ、ツリーごと消える。
    子は一時停止状態で起動しておき、割り当て後に再開する(割り当て前に孫を生まれて Job から漏れないように)。
    """
    job = None
    try:
        job = _k32.CreateJobObjectW(None, None)
        if job:
            info = _JobExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
            if (_k32.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT_INFO,
                                             ctypes.byref(info), ctypes.sizeof(info))
                    and _k32.AssignProcessToJobObject(job, int(proc._handle))):
                return job
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        pass
    if job:
        _k32.CloseHandle(job)
    return None   # 呼び出し側が taskkill にフォールバックする


def _terminate_job(job, limit=5.0):
    """Job 内の全プロセスを止め、実際に消えるまで待つ(最大 limit 秒)。

    TerminateJobObject は非同期で、戻った時点では孫がまだ一時ファイルを握っている(Job の
    ActiveProcesses は先に0になるので目安にならない)。待たないと後片付けで削除に失敗して
    %TEMP% に残るため、先にプロセスハンドルを取っておき、その終了を待つ。
    """
    pids = _JobPidList()
    # 枠に入りきらず失敗しても、入った分は使える
    _k32.QueryInformationJobObject(job, _JOB_BASIC_PROCESS_ID_LIST, ctypes.byref(pids),
                                   ctypes.sizeof(pids), None)
    handles = [h for h in (_k32.OpenProcess(_SYNCHRONIZE, False, pid)
                           for pid in pids.ProcessIdList[:pids.NumberOfProcessIdsInList]) if h]
    _k32.TerminateJobObject(job, 1)
    deadline = time.monotonic() + limit
    for h in handles:
        _k32.WaitForSingleObject(h, max(0, int((deadline - time.monotonic()) * 1000)))
        _k32.CloseHandle(h)


def _kill_tree(proc, job):
    """proc 配下のプロセスツリーを止める。正常終了後(MCPサーバの取り残し防止)にも呼ぶ。"""
    if os.name == "nt":
        if job:
            _terminate_job(job)
            _k32.CloseHandle(job)
        elif proc.poll() is None:
            # Job に入れられなかったときの代替。直接の子が生きているうちでないとツリーを辿れないため、
            # 正常終了後に残った孫は回収できない(固まり自体はファイル受けで起きない)
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def run_process_tree(argv, input_bytes, cwd, timeout, workdir):
    """argv を起動し、直接の子の終了だけを待つ。戻り値: (returncode, stdout, stderr, timed_out)。

    stdin/stdout/stderr は workdir 内のファイルに向ける。孫が握るのはファイルなのでパイプのような
    EOF待ちが起きず、タイムアウト時も途中までの出力が残る。終了後(正常でもタイムアウトでも)
    プロセスツリー全体を kill する。
    """
    stdin_path = os.path.join(workdir, "stdin")
    out_path = os.path.join(workdir, "stdout")
    err_path = os.path.join(workdir, "stderr")
    with open(stdin_path, "wb") as f:
        f.write(input_bytes)

    if os.name == "nt":
        isolation = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED}
    else:
        isolation = {"start_new_session": True}
    # 親側のハンドルは Popen 直後に閉じる(with を抜ける)。子には複製が渡っている
    with open(stdin_path, "rb") as fin, open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
        proc = subprocess.Popen(argv, stdin=fin, stdout=fout, stderr=ferr, cwd=cwd, **isolation)
    job = None
    if os.name == "nt":
        job = _job_attach(proc)
        if _ntdll.NtResumeProcess(int(proc._handle)) != 0:
            _kill_tree(proc, job)
            raise OSError("一時停止で起動した子を再開できない(NtResumeProcess 失敗)")

    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        _kill_tree(proc, job)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass

    def read(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    return proc.returncode, read(out_path), read(err_path), timed_out


def with_last_message(argv, path):
    """codex exec の argv に `--output-last-message <path>` を足して返す(足せない/不要なら元のまま)。

    stdout が空・欠けても最終メッセージをファイルから拾えるようにする。足さない場合:
    PANEL_CODEX_LAST_MESSAGE=0 / `exec` を含まない / 既に -o 系がある。末尾が `-`(stdin指定)なら
    その手前に挿入する。
    """
    if os.environ.get("PANEL_CODEX_LAST_MESSAGE") == "0" or "exec" not in argv:
        return argv
    if any(a in ("-o", "--output-last-message") or a.startswith("--output-last-message=")
           for a in argv):
        return argv
    # cmd /c は引用符が3つ以上あると先頭の引用符を剥がして壊れるため、空白入りの経路は付与を諦める
    if argv[0].lower() == "cmd" and re.search(r"\s", path):
        return argv
    return _insert_exec_opts(argv, ["--output-last-message", path])


def _config_default(argv, key, value):
    """codex exec の argv に `-c key=value` を足して返す(exec でない/既に key を指定済みなら元のまま)。"""
    if "exec" not in argv or any(a.startswith((f"{key}=", f"--config={key}=")) for a in argv):
        return argv
    return _insert_exec_opts(argv, ["-c", f"{key}={value}"])


def without_notify(argv):
    """codex exec の argv に `-c notify=[]` を足して返す(既に notify を指定していれば元のまま)。

    config.toml の notify はターン終了ごとに起動される通知プログラムで、stdio を継承したまま
    残って今回の固まりの原因になった。ヘッドレスのレビューには不要で、常駐サービスを起動されると
    lens 終了時の Job 停止に巻き込むことにもなるので、レビュー用の実行では止める。
    """
    return _config_default(argv, "notify", "[]")


def without_hooks(argv):
    """codex exec の argv に `-c features.hooks=false` を足して返す(既に指定していれば元のまま)。

    ~/.codex/hooks.json には Claude Code から取り込まれた hook(wadachi guard・risky-ops 記録・
    Stop の品質リマインダ・SessionEnd のセッションノート/cc-ops/wadachi 取り込み)と Orca の通知が
    並び、lens 1本ごとに powershell を10個前後起動する。読み取り専用のレビューには不要で、
    レビューの実行が cc-ops/wadachi にセッションとして記録されるノイズにもなるので止める。
    """
    return _config_default(argv, "features.hooks", "false")


def with_unelevated_sandbox(argv):
    """Windows の codex exec に `-c windows.sandbox=unelevated` を足して返す(既に指定していれば元のまま)。

    config.toml の `[windows] sandbox = "elevated"` では、コマンド実行前の setup refresh が
    デスクトップ版の runtime(cua_node の node_repl.exe)の ACL 検証で、実行中ファイルの共有違反
    (os error 32)を致命扱いにして失敗し、レビュアーのシェルが全て `helper_unknown_error: setup
    refresh had errors` で拒否される(codex 0.162.0 時点の既知不具合。デスクトップ版の起動中に起きる)。
    unelevated でも --sandbox read-only の書き込み拒否とネットワーク遮断は効く(2026-10-16 確認)が、
    読み取りは利用者本人の権限になる。elevated に戻すには PANEL_CODEX_CMD に
    `-c windows.sandbox=elevated` を書く。
    """
    if os.name != "nt":
        return argv
    return _config_default(argv, "windows.sandbox", "unelevated")


def _insert_exec_opts(argv, extra):
    """末尾が `-`(stdin指定)ならその手前に、そうでなければ末尾に extra を足す。"""
    if argv[-1] == "-":
        return argv[:-1] + extra + ["-"]
    return argv + extra


def resolve_codex_argv(template):
    """codex の実体をPATHから解決する。戻り値: (argv, 表示名)。未検出なら (None, 名前)。

    npm製の shim は `.cmd` なので CreateProcess から直接起動できない(shutil.which では
    見つかるのに subprocess が FileNotFoundError になる)。cmd /c 経由に包む。
    """
    tokens = template.split()
    exe = shutil.which(tokens[0])
    if exe is None:
        return None, tokens[0]
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe] + tokens[1:], tokens[0]
    return [exe] + tokens[1:], tokens[0]


# codex の実行基盤(サンドボックス helper・コマンド実行)が失敗しても、codex 自体は exit 0 のまま
# 「ファイルを読めなかった」と本文に書いて終わることがある(2026-10-15 に観測。Windows の read-only
# サンドボックスで `helper_unknown_error: setup refresh had errors`)。終了コードでは拾えないので、
# codex にだけ最終行で完了可否を申告させ、未完了をlensの失敗として数える。
# stderr の本文は照合しない: codex が読んだファイルの中身がそのまま流れるため、対象コードに含まれる
# 識別子(このファイル自身など)で誤って失敗にしてしまう。照合するのは codex 自身のログ行だけ。
CODEX_STATUS_INSTRUCTION = (
    "\n\n最後の行に、レビューを完了できたかを次のどちらか1行だけで書く(この行は指摘の形式の例外):\n"
    "REVIEW_STATUS: complete\n"
    "REVIEW_STATUS: incomplete\n"
    "上に示したレビュー対象そのものを確認できなかった場合、または追加の指示で求められた確認を"
    "(ファイルを読めない・ツールが失敗した等で)終えられなかった場合は incomplete。"
    "それ以外は complete。示された対象だけで判断できたなら、追加のファイルを読めなかったことは"
    "それだけでは incomplete にしない。"
)
# codex のログ(tracing 形式)のうち、ツール実行の失敗を示す行。例:
# 2026-10-15T02:48:03.812355Z ERROR codex_core::tools::router: error=exec_command failed: ...
_CODEX_TOOL_ERROR_LINE = re.compile(
    r"(?m)^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+ERROR\s+codex_core::tools::router:\s*(.*)$")
# 行全体が申告であるものだけを数える(引用・コードブロック内の同じ文字列は申告ではない)
_REVIEW_STATUS_LINE = re.compile(r"[ \t]*REVIEW_STATUS:[ \t]*(complete|incomplete)[ \t]*", re.IGNORECASE)
# 申告行が無い応答(古いプロンプトや指示無視)のための予備判定。codex の実行基盤が出す固定の識別子
CODEX_RUNTIME_ERRORS = (
    "helper_unknown_error",
    "Failed to create unified exec process",
    "setup refresh had errors",
)


def codex_tool_failures(stderr):
    """codex 自身のログ行から、失敗したツール実行の回数と識別子を返す。戻り値: (回数, 識別子のリスト)。"""
    lines = _CODEX_TOOL_ERROR_LINE.findall(stderr or "")
    codes = sorted({m for line in lines for m in CODEX_RUNTIME_ERRORS if m in line})
    return len(lines), codes


def codex_review_problem(output):
    """codex の最終出力がレビューとして成立しているか。戻り値: (失敗理由 or None, 申告行を除いた本文)。

    申告行があればそれに従う(complete なら本文に識別子が出ていても成功)。無いときだけ、
    実行基盤の識別子が本文に出ていれば失敗とする。どちらも無ければ従来どおり成功。
    申告行はコードブロック(```)の外で行全体が申告のものだけを数え、最後のものを採る。
    """
    lines = output.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    statuses, kept, fenced = [], [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            m = _REVIEW_STATUS_LINE.fullmatch(line)
            if m:
                statuses.append(m.group(1).lower())
                continue
        kept.append(line)
    body = "\n".join(kept).strip()
    if statuses:
        if statuses[-1] == "incomplete":
            return "codex がレビュー未完了を申告(REVIEW_STATUS: incomplete)", body
        return None, body
    markers = [m for m in CODEX_RUNTIME_ERRORS if m in output]
    if markers:
        return f"codex の実行基盤が失敗({', '.join(markers)})。対象を読めずに書いた可能性がある", body
    return None, body


async def run_codex_lens(lens_name, content, source, instruction, cwd):
    started = time.monotonic()
    template = os.environ.get("PANEL_CODEX_CMD", DEFAULT_CODEX_CMD)
    argv, name = resolve_codex_argv(template)

    def result(output, error):
        return {"lens": lens_name, "backend": "codex", "model": name,
                "seconds": round(time.monotonic() - started, 1),
                "output": output, "error": error}

    if argv is None:
        return result("", f"{name} が見つかりません。`npm i -g @openai/codex` の後 `codex login` が必要。")

    # プロンプトは標準入力から渡す(codex exec の末尾 `-` がstdin読み取りを指示する)
    prompt = SYSTEM_PROMPT + "\n\n" + build_prompt(
        lens_name, LENSES[lens_name]["focus"], content, source, instruction) + CODEX_STATUS_INSTRUCTION

    timeout = CODEX_TIMEOUT   # テストで差し替えられるよう呼び出し時に読む

    def _run():
        with tempfile.TemporaryDirectory(prefix="panel-codex-", ignore_cleanup_errors=True) as workdir:
            last_path = os.path.join(workdir, "last-message.txt")
            review_argv = with_unelevated_sandbox(without_hooks(without_notify(argv)))
            rc, out, err, timed_out = run_process_tree(
                with_last_message(review_argv, last_path), prompt.encode("utf-8"),
                cwd, timeout, workdir)
            out = out.strip()
            if not out and os.path.exists(last_path):   # stdout が空なら最終メッセージで補う
                with open(last_path, encoding="utf-8", errors="replace") as f:
                    out = f.read().strip()
            return rc, out, err, timed_out

    try:
        rc, output, stderr, timed_out = await asyncio.to_thread(_run)
    except OSError as e:   # 起動失敗(実行権限・cwd不正など)。1lensの失敗で全体を落とさない
        return result("", f"codex の起動に失敗: {type(e).__name__}: {e}")

    # codexは起動バナーをstderrに出すため、実際の失敗理由が出る**末尾**を残す
    tail = stderr.strip()[-800:]
    if timed_out:
        return result(output, f"codex がタイムアウト({timeout:g}秒): ...{tail}")
    if rc != 0:
        return result(output, f"codex exit {rc}: ...{tail}")
    problem, body = codex_review_problem(output)
    if problem:
        # 失敗時は本文が表示されないため、何を書いたかの冒頭を理由に添える
        return result(body, f"{problem}。出力冒頭: {body[:300]}")
    failures, codes = codex_tool_failures(stderr)
    if failures:
        # 成功扱いのまま、ファイルを読めずに提示本文だけで書いた可能性を読み手に見せる
        detail = f"({', '.join(codes)})" if codes else ""
        body += (f"\n\n> [panel] codex のツール実行が{failures}回失敗{detail}。"
                 "リポジトリのファイルは読めず、提示した本文だけでレビューした可能性がある。")
    return result(body, None)


async def run_codex_all(lens_names, content, source, instruction, cwd):
    return list(await asyncio.gather(*[
        run_codex_lens(n, content, source, instruction, cwd) for n in lens_names
    ]))


# ---------------------------------------------------------------- スケジューリング

def resolve_models(cfg, lens_names, installed, override):
    """lens名 → 実際に使うmodels.yamlキー。未インストールは FALLBACK 順に降格。"""
    def available(key):
        m = cfg.get("models", {}).get(key)
        if not m:
            return False
        tag = m.get("tag", "")
        return tag in installed or f"{tag}:latest" in installed

    mapping = {}
    for name in lens_names:
        want = override or LENSES[name]["model"]
        if available(want):
            mapping[name] = want
            continue
        mapping[name] = next((k for k in FALLBACK if available(k)),
                             cfg.get("default", "coder"))
    return mapping


async def run_ollama_all(cfg, mapping, content, source, instruction):
    """モデル単位は直列、同一モデル内のlensは並列(VRAMスワップを避ける)。"""
    groups = {}
    for lens_name, key in mapping.items():
        groups.setdefault(key, []).append(lens_name)

    results = []
    for key, lens_names in groups.items():
        results.extend(await asyncio.gather(*[
            run_ollama_lens(cfg, key, n, content, source, instruction) for n in lens_names
        ]))
    return results


# ---------------------------------------------------------------- 出力

def render_markdown(results, source, elapsed):
    ok = [r for r in results if not r["error"]]
    ng = [r for r in results if r["error"]]
    head = (f"# 外部レビューパネル\n\n"
            f"対象: {source} / レビュアー {len(results)}体 / 合計 {elapsed:.0f}秒\n")
    body = []
    for r in sorted(results, key=lambda x: x["lens"]):
        title = f"\n## [{r['lens']}] {r['backend']}:{r['model']} ({r['seconds']}秒)\n"
        body.append(title + (f"\n**失敗**: {r['error']}\n" if r["error"] else f"\n{r['output']}\n"))
    tail = f"\n---\n成功 {len(ok)} / 失敗 {len(ng)}\n"
    return head + "".join(body) + tail


async def cmd_check(cfg):
    alive = await llm.is_alive()
    installed = set(await llm.list_models()) if alive else set()
    print(f"Ollama ({llm.OLLAMA_BASE}): {'UP' if alive else 'DOWN'}")
    if alive:
        for key in sorted(cfg.get("models", {})):
            info = llm.resolve(cfg, key)
            mark = "OK " if (info["tag"] in installed
                             or f"{info['tag']}:latest" in installed) else "未DL"
            print(f"  {mark} {key:9s} {info['tag']} ({info['placement']})")
    argv, name = resolve_codex_argv(os.environ.get("PANEL_CODEX_CMD", DEFAULT_CODEX_CMD))
    if argv is None:
        print(f"codex CLI: NOT FOUND ({name}) — npm i -g @openai/codex && codex login")
    else:
        print(f"codex CLI: {' '.join(argv[:3])}")
        auth = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
        print(f"  ログイン: {'済' if os.path.exists(auth) else '未 (codex login が必要)'}")
    print("\nlens:")
    for name, spec in LENSES.items():
        print(f"  {name:12s} -> {spec['model']}")
    print("preset: " + ", ".join(f"{k}({len(v)})" for k, v in PRESETS.items()))
    return 0 if alive else 1


# ---------------------------------------------------------------- エントリポイント

async def main_async(args):
    cfg = llm.load_config()

    if args.check:
        return await cmd_check(cfg)

    lens_names = ([n.strip() for n in args.lens.split(",") if n.strip()]
                  if args.lens else PRESETS[args.preset])
    unknown = [n for n in lens_names if n not in LENSES]
    if unknown:
        raise SystemExit(f"未知のlens: {', '.join(unknown)} / 選択肢: {', '.join(LENSES)}")

    content, source = collect_content(args.target, args.diff, args.max_chars)
    started = time.monotonic()
    tasks = []

    if args.backend in ("ollama", "both"):
        if not await llm.is_alive():
            raise SystemExit(f"Ollamaに接続できません({llm.OLLAMA_BASE})。`ollama serve` を確認。")
        installed = set(await llm.list_models())
        mapping = resolve_models(cfg, lens_names, installed, args.model)
        tasks.append(run_ollama_all(cfg, mapping, content, source, args.instruction))

    if args.backend in ("codex", "both"):
        cwd = args.diff or (os.path.dirname(os.path.abspath(args.target[0]))
                            if args.target and args.target[0] != "-" else os.getcwd())
        tasks.append(run_codex_all(lens_names, content, source, args.instruction, cwd))

    gathered = await asyncio.gather(*tasks)
    results = [r for group in gathered for r in group]
    elapsed = time.monotonic() - started

    if args.format == "json":
        print(json.dumps({"source": source, "seconds": round(elapsed, 1),
                          "results": results}, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(results, source, elapsed))
    # 全滅時だけでなく、半数以上のlensが失敗した場合も失敗として exit 1 にする
    failed = sum(1 for r in results if r["error"])
    if not results or failed * 2 >= len(results):
        return 1
    return 0


def main():
    p = argparse.ArgumentParser(
        description="ローカルLLM/Codexを並列レビュアーとして走らせる外部レビューパネル")
    p.add_argument("--target", action="append", default=[],
                   help="レビュー対象ファイル(複数可)。'-' で標準入力")
    p.add_argument("--diff", help="このgitリポジトリの `git diff HEAD` を対象にする")
    p.add_argument("--lens", help=f"観点をカンマ区切りで指定: {', '.join(LENSES)}")
    p.add_argument("--preset", default="default", choices=list(PRESETS),
                   help="lens未指定時のプリセット(既定: default)")
    p.add_argument("--backend", default="ollama", choices=["ollama", "codex", "both"])
    p.add_argument("--model", help="全lensのモデルをこのmodels.yamlキーで上書き")
    p.add_argument("--instruction", help="全レビュアーへの追加指示")
    p.add_argument("--max-chars", type=int, default=60000, help="対象本文の最大文字数")
    p.add_argument("--format", default="md", choices=["md", "json"])
    p.add_argument("--check", action="store_true", help="疎通とモデル/lens一覧を表示して終了")
    args = p.parse_args()

    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
