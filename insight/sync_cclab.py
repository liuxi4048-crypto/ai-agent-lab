r"""cc-session-lab(Claude Code のセッション可視化)を insight/cclab/ に取り込み直す。

ソースは別リポジトリ(既定: C:\project\cc-internals-lab\skill\cc-session-lab、
環境変数 CCLAB_SRC で変更可)。ai-agent-lab 側では直接編集せず、更新はこのスクリプトで行う。

  python -I insight/sync_cclab.py
"""
import os
import shutil
from pathlib import Path

SRC = Path(os.environ.get("CCLAB_SRC", r"C:\project\cc-internals-lab\skill\cc-session-lab"))
DEST = Path(__file__).resolve().parent / "cclab"


def main():
    if not (SRC / "scripts" / "lab.py").exists():
        raise SystemExit(f"cc-session-lab が見つかりません: {SRC}")
    shutil.rmtree(DEST, ignore_errors=True)
    for sub in ("scripts", "app", "data"):
        shutil.copytree(SRC / sub, DEST / sub, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (DEST / "__init__.py").write_text("", encoding="utf-8")
    (DEST / "VERSION").write_text("cc-session-lab 1.0.0(insight/sync_cclab.py で取り込み)\n", encoding="utf-8")
    print(f"synced {SRC} -> {DEST}")


if __name__ == "__main__":
    main()
