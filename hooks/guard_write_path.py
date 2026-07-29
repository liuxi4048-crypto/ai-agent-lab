"""Claudeレビュー用 PreToolUse フック: 作業ルート外への書き込みを拒否する。

Claude Code の `--permission-mode acceptEdits` も `--allowedTools "Write(./**)"` も、
実測では**カレントディレクトリ外への書き込みを止めない**(2026-07-29 / CLI 2.1.216 で確認)。
サンドボックスを実際に成立させる唯一の確実な手段がこのフックなので、
claude_review.py は必ず --settings 経由でこれを噛ませて CLI を起動する。

使い方: python guard_write_path.py <許可するルート絶対パス>
stdin に Claude Code から {"tool_name":..., "tool_input":{...}} が渡る。
拒否するときは exit 2 + stderr にお理由(PreToolUseのブロック規約)。
"""
import json
import os
import sys

# file_path 以外の名前でパスを渡すツールにも備えて候補を並べる
PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")


def main() -> int:
    if len(sys.argv) < 2:
        return 0   # ルート未指定なら判定しない(誤ってブロックしない)
    root = os.path.realpath(sys.argv[1])

    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    for key in PATH_KEYS:
        raw = tool_input.get(key)
        if not raw or not isinstance(raw, str):
            continue
        # 相対パスは cwd(=作業ルート)基準。realpath でシンボリックリンクも解決する
        target = os.path.realpath(os.path.join(root, raw))
        if target != root and not target.startswith(root + os.sep):
            sys.stderr.write(
                f"作業ディレクトリの外への書き込みは禁止されています: {raw}\n"
                f"許可されているのは {root} 配下だけです。"
                "成果物のファイルだけを相対パスで編集してください。\n")
            return 2   # PreToolUse: exit 2 = ブロックしてstderrをClaudeへ返す

    return 0


if __name__ == "__main__":
    sys.exit(main())
