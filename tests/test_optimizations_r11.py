"""第11弾の検証: run_command の末尾保持、_collect_files の digest 条件読み。"""
import asyncio
import sys

from orchestrator import _collect_files
from tools import Toolbox

PY = sys.executable


def _tb(tmp_root: str) -> Toolbox:
    tb = Toolbox(approve=False)
    tb.root = tmp_root
    return tb


def test_run_command_keeps_tail_of_huge_output(tmp_path):
    tb = _tb(str(tmp_path))
    # 末尾に目印を置いた大きな出力を出す(シェル非依存にするため python を使う)
    code = "print('x' * 100000); print('TAIL-MARKER')"
    result = asyncio.run(tb.run("run_command", {
        "command": f'{PY} -c "{code}"', "working_dir": "."}))
    assert result.startswith("exit=0")
    assert "TAIL-MARKER" in result            # 末尾は残る
    assert "末尾のみ表示" in result            # 省略が明示される
    assert len(result) < 20000                # 全量は保持しない


def test_run_command_small_output_unchanged(tmp_path):
    tb = _tb(str(tmp_path))
    result = asyncio.run(tb.run("run_command", {
        "command": f'{PY} -c "print(12345)"', "working_dir": "."}))
    assert result.startswith("exit=0")
    assert "12345" in result
    assert "末尾のみ表示" not in result


def test_collect_files_digest_flag(tmp_path):
    (tmp_path / "a.py").write_text("print('hello-digest')\n", encoding="utf-8")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    text, arts = _collect_files(str(tmp_path), digest=False)
    assert "hello-digest" not in text          # 一覧のみの呼び出しでは内容を読まない
    assert "a.py" in text                      # ファイル一覧自体は返る
    assert {a["name"] for a in arts} == {"a.py", "b.bin"}
    text2, _ = _collect_files(str(tmp_path), digest=True)
    assert "hello-digest" in text2
