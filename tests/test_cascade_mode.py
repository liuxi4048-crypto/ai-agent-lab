"""カスケード (2026-10-28): 高速モデル先行 + 決定的品質ゲート + 段階フォールバック。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestrator
from orchestrator import CascadeOrchestrator, _draft_gate

TASK = "中学生向けに、分数の割り算がなぜ逆数をかけるのかを、例題2つ付きで説明して"

GOOD = (
    "分数の割り算は、割る数の逆数を掛けて計算します。たとえば 3/4 ÷ 2/5 は "
    "2/5 の逆数 5/2 を掛けて 15/8 になります。\n\n"
    "例題1: 1/2 ÷ 1/4 は、1/4 の逆数 4 を掛けて 2 になります。\n"
    "例題2: 2/3 ÷ 4/9 は、4/9 の逆数 9/4 を掛けて 3/2 になります。\n\n"
    "理由は、割り算が「いくつ分あるか」を数える操作だからです。"
)

SHORT = "逆数を掛けます。"


def test_gate_passes_good_draft():
    assert _draft_gate(TASK, GOOD) == []


def test_gate_rejects_short():
    assert "短すぎる" in _draft_gate(TASK, SHORT)


def test_gate_rejects_missing_examples():
    one = GOOD.replace("例題2", "練習")
    assert "例題の数が足りない" in _draft_gate(TASK, one)
    # 例題指定のないタスクでは数チェックをしない
    assert _draft_gate("なぜ空は青いの？", GOOD) == []


WRONG_MATH = (
    "分数の割り算を直感で理解しましょう。たとえば "
    "$$\\frac{1}{2} \\div \\frac{1}{4} = 8$$ のように、"
    "1/2 の中に 1/4 が 8 個入ると考えるのがポイントです。"
    "これが割り算の意味であり、逆数を掛ける理由にもつながります。"
)


def test_gate_rejects_wrong_arithmetic():
    reasons = _draft_gate("分数の割り算を例題2つ付きで説明して", WRONG_MATH)
    assert any("計算の誤り" in r for r in reasons)


def test_gate_accepts_correct_arithmetic_forms():
    ok_texts = [
        "$$\\frac{3}{4} \\div \\frac{2}{5} = \\frac{15}{8}$$ です。",
        "$$\\frac{2}{3} \\div \\frac{4}{9} = \\frac{18}{12} = \\frac{3}{2}$$ です。",
        "$$\\frac{3}{5} \\div \\frac{1}{2} = \\frac{6}{5} = 1 \\frac{1}{5}$$ です。",
        "$$\\frac{3}{4} \\times \\frac{5}{2} = \\frac{15}{8}$$ です。",
        # 変形式の連鎖 (最初の = の右辺は結果ではない) は誤検出しない
        "$$\\frac{3}{4} \\div \\frac{1}{5} = \\frac{3}{4} \\times 5 = \\frac{15}{4}$$ です。",
        # 整数で割る形
        "$$\\frac{3}{4} \\div 2 = \\frac{3}{8}$$ です。",
    ]
    for t in ok_texts:
        pad = "説明の本文です。" * 8
        assert not any("計算の誤り" in r for r in _draft_gate("説明して", pad + t)), t


def test_gate_rejects_wrong_fraction_rhs_and_int_operand():
    wrong = [
        "$$\\frac{1}{2} \\div \\frac{1}{4} = \\frac{8}{1}$$ です。",   # 分数形の誤答
        "$$\\frac{3}{4} \\div 2 = \\frac{3}{4}$$ です。",              # 整数割り算の誤答
        "$$\\frac{3}{4} \\times \\frac{5}{2} = \\frac{8}{15}$$ です。",  # 掛け算の誤答
    ]
    for t in wrong:
        pad = "説明の本文です。" * 8
        assert any("計算の誤り" in r for r in _draft_gate("説明して", pad + t)), t


def test_gate_rejects_unclosed_fence_and_mid_sentence():
    assert "コードブロックが閉じていない" in _draft_gate(TASK, GOOD + "\n```python\nx=1")
    assert "文が途中で終わっている可能性がある" in _draft_gate(TASK, GOOD + "そして")


class FakeBus:
    def __init__(self):
        self.nodes = {}
        self._seq = 0
        self.finished = False
        self.error = None
        self.logs = []

    def reset(self):
        pass

    def create_node(self, kind, title, detail, parent_id=None):
        self._seq += 1
        node_id = f"n{self._seq}"
        self.nodes[node_id] = {"kind": kind, "title": title}
        return node_id

    def set_status(self, node_id, status):
        pass

    def emit_log(self, node_id, line):
        self.logs.append(line)

    def complete(self, node_id, output=None, error=None):
        self.nodes[node_id]["output"] = output
        self.nodes[node_id]["error"] = error

    def cancel_all(self, reason):
        pass

    def run_finished(self, error=None):
        self.finished = True
        self.error = error


def _run_cascade(monkeypatch, responder):
    calls = []

    async def fake_stream(bus, node_id, messages, cfg, key, **kwargs):
        calls.append(key)
        return responder(key, messages, len(calls))

    monkeypatch.setattr(orchestrator, "_stream_llm", fake_stream)
    bus = FakeBus()
    orc = CascadeOrchestrator(bus, cfg={}, model="deepseek_strata",
                              fast_model="distill4b")
    asyncio.run(orc.run(TASK))
    answers = [n for n in bus.nodes.values() if n["kind"] == "answer"]
    return bus, calls, answers


def test_cascade_fast_path_single_fast_call(monkeypatch):
    bus, calls, answers = _run_cascade(monkeypatch, lambda k, m, n: GOOD)
    assert calls == ["distill4b"]                    # 高速モデル 1 回だけ
    assert answers[0]["output"] == GOOD
    assert bus.finished and bus.error is None


def test_cascade_repair_path(monkeypatch):
    def responder(key, messages, n):
        if n == 1:
            return SHORT                              # 下書き不合格
        return GOOD                                   # 自己修正で合格

    bus, calls, answers = _run_cascade(monkeypatch, responder)
    assert calls == ["distill4b", "distill4b"]
    assert answers[0]["output"] == GOOD
    assert bus.finished and bus.error is None


def test_cascade_skeleton_expand_path(monkeypatch):
    def responder(key, messages, n):
        if key == "deepseek_strata":
            return "骨格: 理由→例題1→例題2→まとめ。数値 15/8、3/2 を使うこと。"
        if n <= 2:
            return SHORT                              # 下書き・自己修正とも不合格
        return GOOD                                   # 展開は合格

    bus, calls, answers = _run_cascade(monkeypatch, responder)
    assert calls == ["distill4b", "distill4b", "deepseek_strata", "distill4b"]
    assert answers[0]["output"] == GOOD
    assert bus.finished and bus.error is None


def test_cascade_fallback_to_main_full(monkeypatch):
    def responder(key, messages, n):
        if key == "deepseek_strata" and n >= 5:
            return GOOD                               # 最終フォールバック
        if key == "deepseek_strata":
            return "骨格だけ。"
        return SHORT                                  # 下書き・修正・展開とも不合格

    bus, calls, answers = _run_cascade(monkeypatch, responder)
    assert calls == ["distill4b", "distill4b", "deepseek_strata",
                     "distill4b", "deepseek_strata"]
    assert answers[0]["output"] == GOOD
    assert bus.finished and bus.error is None
