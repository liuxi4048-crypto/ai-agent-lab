"""ツール群 + 安全策(パス限定 / denylist / 実行前承認)。

エージェントの「手足」。生成物は WORKSPACE(= projects/)配下に限定する。
安全の本丸は「危険操作の実行前承認(既定ON)」。denylist は補助線(回避容易)。

並列実行対応(v2):
- グローバル APPROVER を廃止し、Run ごとに Toolbox インスタンスを持つ。
  承認コールバックは async callable(command, cwd) -> bool。
- subdir 指定で projects/<run_id>/... をルートにでき、並列コーダー同士の
  ファイル衝突を防ぐ(swarm-code は sub_0/ sub_1/ ... を割り当てる)。
- run_command は asyncio.create_subprocess_shell + wait_for(タイムアウト)。
"""
import asyncio
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(BASE, "projects")

# denylist は回避容易(例 python -c "shutil.rmtree(...)")。あくまで明白な事故の一次防止。
# 本当の安全は承認モード(既定ON)と、発展のコンテナ/別ユーザ隔離で担保する。
DENY_PATTERNS = [
    r"\brm\s+-rf?\b", r"\brmdir\s+/s\b", r"\bdel\s+/[sqf]", r"\brd\s+/s\b",
    r"\bformat\b", r"\bmkfs\b", r"\bdiskpart\b", r"\bdd\s+if=",
    r"git\s+reset\s+--hard", r"git\s+clean\s+-[a-z]*f", r"push\s+.*(--force|-f)\b",
    r"\bshutdown\b", r"\breboot\b", r"\btaskkill\s+/f", r"\breg\s+delete\b",
    r"shutil\.rmtree", r":\s*>\s*/", r">\s*/dev/sd",
]
DENY_RE = re.compile("|".join(DENY_PATTERNS), re.IGNORECASE)


class Toolbox:
    """1つの Run(エージェント実行)が使うツール一式。

    subdir: WORKSPACE 配下の追加ルート(例 "run_ab12/sub_0")。"" なら projects/ 直下。
    approve: run_command の実行前承認を求めるか。
    approver: async callable(command, cwd) -> bool。None かつ approve=True ならコンソール承認。
    """

    def __init__(self, subdir="", approve=True, approver=None):
        root = os.path.normpath(os.path.join(WORKSPACE, subdir)) if subdir else WORKSPACE
        if not (root == WORKSPACE or root.startswith(WORKSPACE + os.sep)):
            raise ValueError(f"subdir が projects/ の外を指している: {subdir}")
        self.root = root
        self.approve = approve
        self.approver = approver

    def _safe_path(self, path):
        """root 配下に正規化・限定。外に出る指定は拒否。

        パスは root からの相対として扱う。モデルが自然に "projects/foo" と
        書いても二重にならないよう、先頭の冗長な "projects/" は吸収する。
        """
        path = (path or ".").replace("\\", "/").lstrip("/")
        while path == "projects" or path.startswith("projects/"):
            path = path[len("projects"):].lstrip("/") or "."
        p = os.path.normpath(os.path.join(self.root, path))
        if not (p == self.root or p.startswith(self.root + os.sep)):
            raise ValueError(f"作業ディレクトリの外は禁止: {path}")
        return p

    # ---- 個々のツール実装 ----------------------------------------------------
    def list_dir(self, path="."):
        p = self._safe_path(path)
        if not os.path.exists(p):
            return f"(存在しない: {path})"
        if os.path.isfile(p):
            return f"(ファイル) {path}"
        items = []
        for name in sorted(os.listdir(p)):
            full = os.path.join(p, name)
            items.append(name + ("/" if os.path.isdir(full) else ""))
        return "\n".join(items) or "(空)"

    def read_file(self, path):
        p = self._safe_path(path)
        if not os.path.isfile(p):
            return f"(存在しない: {path})"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def write_file(self, path, content):
        p = self._safe_path(path)
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return f"書き込み完了: {path} ({len(content)} 文字)"

    async def run_command(self, command, working_dir=".", timeout=120):
        if DENY_RE.search(command):
            return f"[拒否] 破壊的コマンドの疑い(denylist): {command}"
        cwd = self._safe_path(working_dir)
        os.makedirs(cwd, exist_ok=True)
        if self.approve:
            ok = await (self.approver(command, cwd) if self.approver
                        else _console_approve(command, cwd))
            if not ok:
                return "[スキップ] ユーザーが実行を承認しなかった"
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                return f"[timeout] {timeout}s 超過: {command}"
        except OSError as e:
            return f"[実行エラー] {command}: {e}"
        finally:
            # timeout・Runキャンセル(CancelledError)いずれでも子プロセスを残さない
            if proc is not None and proc.returncode is None:
                proc.kill()
        out = stdout.decode("utf-8", errors="replace")[-4000:]
        err = stderr.decode("utf-8", errors="replace")[-2000:]
        return f"exit={proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"

    async def run(self, name, args):
        """ツール名 + 引数(dict)を実行して文字列結果を返す。finish は None。"""
        if name == "finish":
            return None  # 呼び出し側で終了扱い
        if not isinstance(args, dict):
            return f"[引数エラー] {name}: 引数がオブジェクトでない: {args!r}"
        try:
            if name == "run_command":
                return await self.run_command(**args)
            fn = {"list_dir": self.list_dir, "read_file": self.read_file,
                  "write_file": self.write_file}.get(name)
            if fn is None:
                return f"[不正ツール] 未知のツール名: {name}(利用可: list_dir, read_file, write_file, run_command, finish)"
            return fn(**args)
        except TypeError as e:
            return f"[引数エラー] {name}: {e}"
        except ValueError as e:
            return f"[パスエラー] {name}: {e}"
        except Exception as e:
            return f"[実行エラー] {name}: {e}"


async def _console_approve(command, cwd):
    print(f"\n[承認要求] 実行しますか?\n  $ {command}\n  (cwd={os.path.relpath(cwd, BASE)})")
    ans = input("  y/N > ").strip().lower()
    return ans in ("y", "yes")


# ---- function-calling スキーマ(Ollama native /api/chat 互換) ----------------
TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "作業ディレクトリ配下の一覧",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "作業ルートからの相対パス"}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "ファイル内容を読む",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "ファイルを作成/上書き",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "作業ディレクトリ配下でシェルコマンドを実行(導入/実行/テスト)",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "working_dir": {"type": "string", "description": "作業ルートからの相対。既定 '.'"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "finish", "description": "目標達成時に呼ぶ。要約を渡す",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]
