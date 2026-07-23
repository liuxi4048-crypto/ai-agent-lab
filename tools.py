"""ツール群 + 安全策(パス限定 / denylist / 実行前承認)。

エージェントの「手足」。生成物は WORKSPACE(= projects/)配下に限定する。
安全の本丸は「危険操作の実行前承認(既定ON)」。denylist は補助線(回避容易)。
"""
import os
import re
import json
import shlex
import subprocess

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


def _safe_path(path):
    """WORKSPACE(=projects/)配下に正規化・限定。外に出る指定は拒否。

    パスは projects/ からの相対として扱う。モデルが自然に "projects/foo" と
    書いても二重にならないよう、先頭の冗長な "projects/" は吸収する。
    """
    path = (path or ".").replace("\\", "/").lstrip("/")
    while path == "projects" or path.startswith("projects/"):
        path = path[len("projects"):].lstrip("/") or "."
    p = os.path.normpath(os.path.join(WORKSPACE, path))
    if not (p == WORKSPACE or p.startswith(WORKSPACE + os.sep)):
        raise ValueError(f"projects/ の外は禁止: {path}")
    return p


# ---- 個々のツール実装 -------------------------------------------------------
def list_dir(path="."):
    p = _safe_path(path)
    if not os.path.exists(p):
        return f"(存在しない: {path})"
    if os.path.isfile(p):
        return f"(ファイル) {path}"
    items = []
    for name in sorted(os.listdir(p)):
        full = os.path.join(p, name)
        items.append(name + ("/" if os.path.isdir(full) else ""))
    return "\n".join(items) or "(空)"


def read_file(path):
    p = _safe_path(path)
    if not os.path.isfile(p):
        return f"(存在しない: {path})"
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_file(path, content):
    p = _safe_path(path)
    os.makedirs(os.path.dirname(p) or WORKSPACE, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return f"書き込み完了: {path} ({len(content)} 文字)"


# 承認の入口。GUI 等が APPROVER = callable(command, cwd)->bool を設定すると、
# コンソールの input() の代わりにそれが使われる。None ならコンソール承認。
APPROVER = None


def run_command(command, working_dir=".", timeout=120, approve=True):
    if DENY_RE.search(command):
        return f"[拒否] 破壊的コマンドの疑い(denylist): {command}"
    cwd = _safe_path(working_dir)
    os.makedirs(cwd, exist_ok=True)
    if approve:
        if APPROVER is not None:
            if not APPROVER(command, cwd):
                return "[スキップ] ユーザーが実行を承認しなかった"
        else:
            print(f"\n[承認要求] 実行しますか?\n  $ {command}\n  (cwd={os.path.relpath(cwd, BASE)})")
            ans = input("  y/N > ").strip().lower()
            if ans not in ("y", "yes"):
                return "[スキップ] ユーザーが実行を承認しなかった"
    try:
        r = subprocess.run(command, cwd=cwd, shell=True, capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return f"[timeout] {timeout}s 超過: {command}"
    out = (r.stdout or "")[-4000:]
    err = (r.stderr or "")[-2000:]
    return f"exit={r.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


# ---- OpenAI function-calling スキーマ ---------------------------------------
TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "projects配下のディレクトリ一覧",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "projects からの相対パス"}}}}},
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
        "name": "run_command", "description": "projects配下でシェルコマンドを実行(導入/実行/テスト)",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "working_dir": {"type": "string", "description": "projects からの相対。既定 '.'"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "finish", "description": "目標達成時に呼ぶ。要約を渡す",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]

DISPATCH = {
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
}


def run_tool(name, args, approve=True):
    """ツール名 + 引数(dict)を実行して文字列結果を返す。未知名/失敗は説明を返す。"""
    if name == "finish":
        return None  # 呼び出し側で終了扱い
    fn = DISPATCH.get(name)
    if fn is None:
        return f"[不正ツール] 未知のツール名: {name}(利用可: {', '.join(DISPATCH)}, finish)"
    try:
        if name == "run_command":
            args = {**args, "approve": approve}
        return fn(**args)
    except TypeError as e:
        return f"[引数エラー] {name}: {e}"
    except Exception as e:
        return f"[実行エラー] {name}: {e}"
