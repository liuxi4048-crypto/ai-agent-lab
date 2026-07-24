"""成果物の保存: 最終回答に含まれる「完成ファイル」をworkspaceフォルダに書き出す。

会話や中間結果からの機械的なコード抽出は行わない。
アプリ・スクリプトなど使用可能な成果物が発生したタスクに限り、
LLMがファイル名を明示したコードブロックだけを実ファイルとして保存する。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

# exe化(PyInstaller)時はexeの隣、通常実行時はこのファイルの隣にworkspaceを作る
_BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
WORKSPACE = _BASE / "workspace"

EXT_MAP = {
    "python": "py", "py": "py", "javascript": "js", "js": "js",
    "typescript": "ts", "html": "html", "css": "css", "json": "json",
    "java": "java", "c": "c", "cpp": "cpp", "csharp": "cs", "sql": "sql",
    "bash": "sh", "sh": "sh", "powershell": "ps1", "yaml": "yml", "xml": "xml",
}

# 無名ブロックへの既定ファイル名（コード作成タスクのフォールバック用）
DEFAULT_NAME = {
    "py": "main.py", "js": "script.js", "ts": "main.ts",
    "html": "index.html", "css": "style.css", "sh": "run.sh", "ps1": "run.ps1",
}

# ```python:app.py 形式（フェンス情報にファイル名）
_FENCE_NAMED = re.compile(r"```(\w+)[:\s]+(\S+?\.\w+)\s*\n(.*?)```", re.DOTALL)
# 直前行の「ファイル: app.py」「File: app.py」「filename: app.py」形式（太字・見出し記号は無視）
_LINE_NAMED = re.compile(
    r"^[#*\s]*(?:ファイル|file(?:name)?)\s*[:：]\s*[`\"']?(\S+?\.\w+)[`\"']?[*\s]*$"
    r"\n+```(\w*)\n(.*?)```",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)


def _safe_name(text: str, limit: int = 24) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", text.strip())[:limit].strip("_")
    return name or "output"


def _safe_filename(name: str) -> str:
    """ファイル名を安全な basename に正規化する（パス区切り・親参照を除去）。"""
    base = re.sub(r"[\\/]+", "/", name.strip()).split("/")[-1]
    base = re.sub(r'[:*?"<>|\s]+', "_", base).lstrip(".")
    return base


def extract_files(text: str) -> list[tuple[str, str]]:
    """ファイル名が明示されたコードブロックを (ファイル名, コード) で返す。

    同名ファイルが複数回現れた場合は後のもの（改訂版）を採用する。
    """
    found: dict[str, str] = {}
    for m in _LINE_NAMED.finditer(text):
        name = _safe_filename(m.group(1))
        if name:
            found[name] = m.group(3)
    for m in _FENCE_NAMED.finditer(text):
        name = _safe_filename(m.group(2))
        if name:
            found[name] = m.group(3)
    return list(found.items())


def _fallback_files(final: str) -> list[tuple[str, str]]:
    """無名コードブロックに言語別の既定名を付ける（コード作成タスク専用の保険）。"""
    files: list[tuple[str, str]] = []
    used: set[str] = set()
    for i, (lang, code) in enumerate(_CODE_BLOCK.findall(final), 1):
        ext = EXT_MAP.get(lang.lower())
        if ext is None:
            continue  # 言語不明のブロックは成果物とみなさない
        name = DEFAULT_NAME.get(ext, f"file_{i}.{ext}")
        if name in used:
            stem, _, suffix = name.rpartition(".")
            name = f"{stem}_{i}.{suffix}"
        used.add(name)
        files.append((name, code))
    return files


def save_artifacts(task: str, final: str, run_id: str = "",
                   allow_unnamed: bool = False) -> list[dict]:
    """最終回答から完成ファイルだけを保存する。

    - ファイル名が明示されたコードブロックのみを成果物とする。
    - allow_unnamed=True（プランナーがコード作成タスクと判定した場合）に限り、
      明示がなければ無名ブロックへ既定名を付けて保存する。
    - 使用可能な成果物がなければ何も保存しない（返り値は空リスト）。
    返り値: [{"name": ファイル名, "path": "/workspace/... のURLパス"}]
    """
    files = extract_files(final)
    if not files and allow_unnamed:
        files = _fallback_files(final)
    if not files:
        return []

    prefix = f"{run_id}_" if run_id else time.strftime("%Y%m%d-%H%M%S_")
    run_dir = WORKSPACE / f"{prefix}{_safe_name(task)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    for name, code in files:
        (run_dir / name).write_text(code, encoding="utf-8")
        saved.append({"name": name, "path": f"/workspace/{run_dir.name}/{name}"})
    return saved
