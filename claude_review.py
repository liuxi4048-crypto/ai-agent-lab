"""Claudeによる最終レビュー: 成果物を読み、直接修正して仕上げる。

**APIキー(従量課金)ではなく、ローカルにインストール済みの Claude Code CLI を
サブスクリプション認証のまま呼ぶ**。したがって:

- API利用料は発生しない。代わりに Claude のサブスク利用枠(5時間ローリング)を消費する。
- サブプロセスの環境からは `ANTHROPIC_API_KEY` を必ず取り除く。残っているとCLIが
  そちらを優先し、意図せず従量課金になるため。
- 作業ディレクトリを `projects/run_<id>/` に固定して起動する。Claude Code は既定で
  cwd 配下しか触れないので、これがそのままサンドボックスになる。
- `Bash` は渡さない(ローカルでの任意コマンド実行権を与えない)。読む・書く・直すのみ。
- 修正後は既存のローカル検証(verify_deliverable / verify_runtime)を必ず通し、
  通らなければ同じセッションを再開して直させる。

CLI は `-p --output-format stream-json --verbose` で起動し、1行1JSONのイベントを
逐次パースしてダッシュボードのログへ流す(数分かかる工程なので無言にしない)。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys

GUARD_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "hooks", "guard_write_path.py")

# 'opus' 等のエイリアス、または完全なモデル名
MODEL = os.environ.get("CLAUDE_REVIEW_MODEL", "opus")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT = os.environ.get("CLAUDE_REVIEW_EFFORT", "high")
if EFFORT not in _EFFORTS:
    EFFORT = "high"

MAX_TURNS = 40            # CLIの1パスあたりのターン上限
TIMEOUT = 1800            # 1パスの上限秒(レビュー+修正は数分かかる)
MAX_FINISH_REJECTS = 2    # ローカル検証NGで差し戻す回数の上限
STREAM_LIMIT = 16 * 1024 * 1024   # 1行が巨大になりうる(Writeツールの入力=ファイル全文)

# Claude Code に許可するツール。シェル / Web / サブエージェントは渡さない。
# 注意: --allowedTools は「自動承認する対象」であって利用可能ツールの限定ではない。
# シェルを止めるには --disallowedTools で名前を挙げて拒否する必要がある。
# さらにシェルツールの名前はOSで異なる(Windows=PowerShell / それ以外=Bash)ため、
# 取りこぼすと任意コマンドを実行できてしまう。実測で確認済みなので両方必ず入れる。
ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep"
DISALLOWED_TOOLS = ",".join((
    "Bash", "PowerShell", "BashOutput", "KillShell", "KillBash",
    "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit",
))

SYSTEM = """あなたはローカルLLMが作った成果物を仕上げる最終レビュアーです。
カレントディレクトリが成果物のルートです。ファイルを実際に読み、問題を見つけ、その場で直してください。

【最重要】
- 指摘だけで終わらせない。Edit / Write で実際に修正すること。
- 手順書・README・レビュー報告書などのファイルを新しく作らない。求められているのは動く成果物です。
- 既存ファイルの修正には Edit を使う(Write の全文書き直しで既存実装を削らない)。
- 外部URL(CDN)への依存を増やさない。ネット接続なしで動く必要があります。
- 動作を変える大規模な作り直しはしない。壊れている所・足りない所を直すのが仕事です。

【見る観点】
1. 正確性: そのまま実行して落ちないか。未定義の変数・関数、存在しないDOM要素の参照、境界条件。
2. 依頼との一致: 依頼された機能が実際に実装されているか(名前だけの空実装になっていないか)。
3. 使いやすさ: 開いた/起動した瞬間に動き出すか。操作方法が画面から分かるか。
4. 簡素化: 使われていないコード・重複を削る。

【最後の応答】
修正を終えたら、日本語で「見つけた問題」と「行った修正」を箇条書きにして返すこと。
問題が無ければ何も直さず、その旨を返してよい。"""

_cli_cache: str | None | bool = False   # False=未判定 / None=見つからない / str=実行パス


def _candidates():
    """Claude Code CLI の実行ファイル候補。PATH上のものが壊れている場合に備えて複数見る。"""
    env = os.environ.get("CLAUDE_CLI")
    if env:
        yield env
    which = shutil.which("claude")
    if which:
        yield which
    appdata = os.environ.get("APPDATA")
    if appdata:
        yield os.path.join(appdata, "npm", "claude.cmd")
    home = os.path.expanduser("~")
    yield os.path.join(home, ".local", "bin", "claude")
    yield "claude"


def _resolve_cli() -> str | None:
    """実際に起動できる CLI のパスを1つ返す。見つからなければ None。

    PATHの先頭に壊れたシムが置かれていることがあるため、`--version` が通るかまで確認する。
    """
    global _cli_cache
    if _cli_cache is not False:
        return _cli_cache
    seen = set()
    for cand in _candidates():
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            # 壊れたシムはOSのコードページ(cp932等)でエラーを吐くため、
            # locale任せの text=True にせずUTF-8で寛容にデコードする
            proc = subprocess.run([cand, "--version"], capture_output=True,
                                  timeout=30, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and "Claude Code" in (proc.stdout or ""):
            _cli_cache = cand
            return cand
    _cli_cache = None
    return None


def status() -> dict:
    """UI用: Claudeレビューが使えるか。使えない理由も日本語で返す。"""
    cli = _resolve_cli()
    if cli is None:
        return {"available": False, "model": MODEL, "cli": None,
                "reason": "Claude Code CLI が見つかりません"
                          "(npm install -g @anthropic-ai/claude-code、または環境変数 CLAUDE_CLI で指定)"}
    return {"available": True, "model": MODEL, "cli": cli, "reason": ""}


def _child_env() -> dict:
    """サブプロセス用の環境。APIキーを取り除きサブスク認証を使わせる。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    return env


class _Pass:
    """CLI 1回分の実行結果。"""

    def __init__(self):
        self.text = ""
        self.session_id = ""
        self.edits = 0
        self.tokens = 0
        self.cost = 0.0
        self.error: str | None = None


def _handle_event(data: dict, out: _Pass, say) -> None:
    """stream-json の1イベントを解釈してログへ流す。"""
    kind = data.get("type")
    if kind == "assistant":
        for block in data.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                body = (block.get("text") or "").strip()
                if body:
                    say(body[:2000])
            elif btype == "tool_use":
                name = block.get("name", "?")
                target = (block.get("input") or {}).get("file_path", "")
                if name in ("Write", "Edit"):
                    out.edits += 1
                say(f"→ {name} {os.path.basename(str(target))}".rstrip())
    elif kind == "result":
        out.text = str(data.get("result") or "").strip()
        out.session_id = data.get("session_id") or out.session_id
        out.cost += float(data.get("total_cost_usd") or 0)
        usage = data.get("usage") or {}
        # inputもサブスク枠を消費するため加算する(outputのみでは過小表示になる)
        out.tokens += int(usage.get("output_tokens") or 0) + int(usage.get("input_tokens") or 0)
        if data.get("is_error") or data.get("subtype") != "success":
            out.error = f"CLIがエラーを返しました ({data.get('subtype')}): {out.text[:200]}"
    elif kind == "system" and data.get("subtype") == "init":
        out.session_id = data.get("session_id") or out.session_id


def _guard_settings(root: str) -> str:
    """作業ルート外への書き込みを拒否する PreToolUse フックの設定JSON(文字列)。

    --permission-mode も --allowedTools のパス指定も cwd 外の書き込みを止めないことを
    実測で確認しているため、サンドボックスはこのフックで担保する。
    """
    command = f'"{sys.executable}" "{GUARD_HOOK}" "{root}"'
    return json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Write|Edit|NotebookEdit",
        "hooks": [{"type": "command", "command": command}],
    }]}})


def _write_settings_file(root: str) -> str:
    """設定JSONを一時ファイルに書き出してパスを返す。

    CLIが .cmd シムだと cmd.exe を経由するため、JSONを引数へ直に渡すと
    引用符・バックスラッシュが壊れて起動に失敗する(実測)。ファイル経由なら安全。
    """
    import tempfile
    fd, path = tempfile.mkstemp(prefix="agentlab_claude_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_guard_settings(root))
    return path


async def _run_cli(exe: str, prompt: str, cwd: str, say, should_stop,
                   resume: str = "") -> _Pass:
    """CLIを1回起動し、stream-json を逐次読みながら結果をまとめる。"""
    out = _Pass()
    settings_path = _write_settings_file(cwd)
    args = ["-p", "--output-format", "stream-json", "--verbose",
            "--settings", settings_path,
            "--model", MODEL, "--effort", EFFORT,
            "--max-turns", str(MAX_TURNS),
            "--permission-mode", "acceptEdits",
            "--allowedTools", ALLOWED_TOOLS,
            "--disallowedTools", DISALLOWED_TOOLS,
            "--strict-mcp-config",
            "--append-system-prompt", SYSTEM]
    if resume:
        args += ["--resume", resume]

    proc = await asyncio.create_subprocess_exec(
        exe, *args, cwd=cwd, env=_child_env(), limit=STREAM_LIMIT,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)

    err_parts: list[bytes] = []

    async def feed():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        finally:
            proc.stdin.close()

    async def drain_err():
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            err_parts.append(line)

    async def pump():
        while True:
            if should_stop and should_stop():
                out.error = "中断されました"
                return
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                out.error = "CLIの出力が大きすぎて読み取れませんでした"
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                _handle_event(json.loads(text), out, say)
            except (ValueError, TypeError, KeyError):
                continue   # JSON以外の行(進捗表示など)は無視

    async def watchdog():
        """should_stop を1秒間隔で監視し、検知した瞬間にプロセスをkillする。

        pump() は proc.stdout.readline() で長時間ブロックしうるため、中断検知を
        pump() 側の判定だけに任せると kill が finally まで遅延し、最大 TIMEOUT
        (1800秒)までハングする。ここで独立に kill することで即時中断にする。
        """
        while True:
            await asyncio.sleep(1)
            if should_stop and should_stop():
                out.error = out.error or "中断されました"
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass   # 直前に自然終了していた(killは不要)
                return

    watchdog_task = asyncio.create_task(watchdog())
    try:
        await asyncio.wait_for(
            asyncio.gather(feed(), pump(), drain_err()), timeout=TIMEOUT)
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        out.error = out.error or f"{TIMEOUT}秒を超えたため中断しました"
    finally:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass   # watchdog内の例外でfinallyの後始末(設定ファイル削除等)を止めない
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.shield(proc.wait())
            except BaseException:
                pass
        try:
            os.remove(settings_path)
        except OSError:
            pass

    if out.error is None and proc.returncode not in (0, None):
        err = b"".join(err_parts).decode("utf-8", errors="replace").strip()
        out.error = f"CLIが異常終了しました (exit={proc.returncode}): {err[:300]}"
    return out


def _sanitize_for_cli(reason: str) -> str:
    """ローカル検証の差し戻し文をClaude CLI向けに書き換える。

    `finish` はローカルエージェント(tools.py)のツール名で、Claude CLI には
    存在せず無意味なため、CLIへ渡す直前だけ置換する
    (ダッシュボードのログ表示 = say() 側はローカルの文脈のまま残してよい)。
    """
    text = reason.replace("[finish拒否]", "[要修正]")
    text = text.replace("finish してください", "修正を完了してください")
    text = text.replace("finish すること", "修正を完了してください")
    return text


async def review_and_fix(*, task: str, deliverable: str | None, toolbox,
                         emit=None, should_stop=None, summary_before: str = "",
                         max_iter: int = MAX_FINISH_REJECTS) -> dict:
    """成果物をClaudeにレビューさせ、その場で修正させる。

    返り値: {"ok", "summary", "edits", "tokens", "cost", "error"}
    - ok=True かつ error=None なら最終成果物として提出できる状態。
    - 例外は投げず error に理由を入れて返す(レビュー失敗でRun全体を落とさない)。
    """
    result = {"ok": False, "summary": "", "edits": 0, "tokens": 0,
              "cost": 0.0, "error": None}
    say = emit or (lambda _line: None)

    exe = _resolve_cli()
    if exe is None:
        result["error"] = status()["reason"]
        return result

    prompt = (f"ユーザーの依頼:\n{task}\n\n"
              f"ローカルエージェントの作業要約:\n{(summary_before or '(なし)')[:4000]}\n\n"
              "カレントディレクトリの成果物をレビューし、問題があれば直接修正してください。")
    if deliverable:
        kind = {"html": "ブラウザで index.html を開けば動くHTMLアプリ",
                "exe": "Windowsの実行ファイル(exe)",
                "script": "run.bat から起動できるスクリプト"}.get(deliverable, deliverable)
        prompt += f"\n\n想定している成果物の形式: {kind}"

    session = ""
    try:
        for attempt in range(max_iter + 1):
            if should_stop and should_stop():
                result["error"] = "中断されました"
                return result

            out = await _run_cli(exe, prompt, toolbox.root, say, should_stop,
                                 resume=session)
            result["edits"] += out.edits
            result["tokens"] += out.tokens
            result["cost"] += out.cost
            if out.text:
                result["summary"] = out.text
            if out.error:
                result["error"] = out.error
                return result

            session = out.session_id
            # ローカル検証を通す。通らなければ同じセッションを再開して直させる
            reason = (toolbox.verify_deliverable(deliverable)
                      or await toolbox.verify_runtime(deliverable))
            if reason is None:
                result["ok"] = True
                return result
            if attempt >= max_iter or not session:
                result["error"] = f"修正後もローカル検証を通りませんでした: {reason[:200]}"
                return result
            say(f"  {reason}")
            prompt = _sanitize_for_cli(reason) + "\n\nこの問題を直してから終えてください。"

        result["error"] = "レビューが所定の回数で終わりませんでした"
        return result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
