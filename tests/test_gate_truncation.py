"""決定的ゲートの打ち切りすり抜け対策と、最終回答の空チェック (2026-11-01)。

契機は2件の実測:
1. cascade 骨格段の実機検証 (report-cascade-skeleton-empty-check.md) で、
   上限到達で打ち切られた自己修正が、末尾文字が _ANSWER_ENDINGS に含まれる
   というだけの都合で _draft_gate を通過し、そのまま最終回答に採用された。
   ゲートの「文が途中で終わっている」判定は末尾文字のみに拠り、打ち切りの
   事実 (done_reason=length) は _stream_llm がログに残すだけで呼び出し側へ
   返していなかった。対策: _stream_llm に meta_out を追加して打ち切りを
   伝搬し、_draft_gate は truncated=True を無条件の不合格理由にする。
2. Run ストア 146 件中 4 件で、最終回答 0 字のまま Run が done になって
   いた (エンジン異常の cascade 3991f92d28、思考暴走の direct、Anthropic
   上限の direct x2。いずれも 2026-10-29〜31 で個別原因は対策済み)。
   対策: 完了直前に _require_final_answer で一律に失敗へ倒す。
   LLM ストリームを fake に差し替えて検証する。
"""
import asyncio

import pytest

import orchestrator
from orchestrator import (
    CascadeOrchestrator, DirectOrchestrator, _draft_gate, _require_final_answer)


class FakeBus:
    def __init__(self):
        self.logs = []
        self.completed = {}
        self.finished = "not-called"
        self.nodes = []

    def reset(self):
        pass

    def create_node(self, kind, title, detail="", parent_id=None):
        self.nodes.append(kind)
        return f"node-{kind}"

    def complete(self, node_id, output=None, error=None):
        self.completed[node_id] = output

    def run_finished(self, error=None):
        self.finished = error

    def cancel_all(self, message):
        pass

    def set_title(self, node_id, title):
        pass

    def set_progress(self, node_id, done, total):
        pass

    def set_prompt(self, node_id, prompt):
        pass

    def set_model(self, node_id, key, tag):
        pass

    def set_status(self, node_id, status):
        pass

    def think_progress(self, node_id, piece):
        pass

    def token_progress(self, node_id, piece):
        pass

    def set_tokens(self, node_id, n):
        pass

    def emit_log(self, node_id, line):
        self.logs.append(line)


def _cfg():
    return {"models": {
        "m1": {"tag": "fake:1", "think": True, "num_ctx": 16384},
        "fast": {"tag": "fake-fast:1", "think": False, "num_ctx": 16384},
    }}


_TASK = "分数の割り算の仕組みを説明してください"

# _draft_gate を通過できる長文の日本語回答 (80 字以上・文完結・検算対象なし)
_LONG_ANSWER = ("これは分数の割り算について説明した最終回答です。"
                "分子と分母を入れ替えて掛け算に直す理由を、具体的な場面に"
                "当てはめながら順を追って説明しています。読み手が迷わない"
                "ように、手順を一つずつ整理して示し、最後に注意点として、"
                "途中の約分を忘れずに行うことが大切であると付け加えます。")
_REPAIR_ANSWER = ("これは自己修正によって書き直された最終回答です。"
                  "下書きで不足していた説明を補い、分数の割り算の手順を"
                  "最初から最後まで通して示しています。読み手が途中で"
                  "つまずかないように、各手順の意味をその都度言葉で"
                  "補足しながら、丁寧に説明を進めていく構成にしました。")
_EXPAND_ANSWER = ("これは骨格に沿って展開された最終回答です。"
                  "骨格の要点をすべて含み、具体例も挙げて説明しています。"
                  "読み手が迷わないように、手順を順番に整理して示します。"
                  "最後に注意点として、途中の計算を省略せず一行ずつ確認する"
                  "ことが大切であると付け加えておきます。")


# ------------------------------------------------------------ ゲート単体

def test_gate_rejects_truncated_even_when_text_looks_complete():
    # 実測の再現: 本文は長文で末尾も「。」なのに、上限到達で切れている
    reasons = _draft_gate(_TASK, _LONG_ANSWER, truncated=True)
    assert reasons == ["生成が長さ上限で打ち切られた"]


def test_gate_accepts_same_text_when_not_truncated():
    # 回帰: 打ち切りでない同一本文は従来どおり合格 (理由なし)
    assert _draft_gate(_TASK, _LONG_ANSWER, truncated=False) == []


def test_gate_default_argument_unchanged():
    # 既定引数 (truncated 未指定) の従来呼び出しと互換
    assert _draft_gate(_TASK, _LONG_ANSWER) == []
    assert _draft_gate(_TASK, "短い") != []


# ------------------------------------------------------- 空チェック単体

def test_require_final_answer_passes_nonempty():
    assert _require_final_answer("回答") == "回答"
    assert _require_final_answer(" あ ") == " あ "


def test_require_final_answer_rejects_empty():
    for empty in ("", "  \n  ", None):
        with pytest.raises(RuntimeError, match="最終回答が空でした"):
            _require_final_answer(empty)


# ------------------------------------------------------- cascade の fake

def _is_expand_call(messages):
    return any(orchestrator.CASCADE_EXPAND_SYSTEM == (m.get("content") or "")
               for m in messages if m.get("role") == "system")


def _chunks(spec):
    """spec: ("text", 本文, truncated) / ("empty",) から chunk 列を作る。"""
    if spec[0] == "empty":
        # 思考だけ流れて本文 0 字で終わる引き (実績の空回答の再現)
        yield {"thinking": "考え中"}
        yield {"done": True, "meta": {"eval_count": 24}}
        return
    _, text, truncated = spec
    yield {"content": text}
    meta = {"eval_count": 2048 if truncated else 100}
    if truncated:
        meta["done_reason"] = "length"
    yield {"done": True, "meta": meta}


def _run_cascade(monkeypatch, task, draft, repair, skeleton, expand, fallback):
    script = {"draft": draft, "repair": repair, "skeleton": skeleton,
              "expand": expand, "fallback": fallback}
    fast_calls = {"n": 0}

    async def fake_chat_stream(cfg, key, messages, **kwargs):
        if key == "fast":
            if _is_expand_call(messages):
                spec = script["expand"]
            else:
                fast_calls["n"] += 1
                spec = script["draft"] if fast_calls["n"] == 1 else script["repair"]
        elif kwargs.get("think") is False:
            spec = script["skeleton"]
        else:
            spec = script["fallback"]
        for c in _chunks(spec):
            yield c

    monkeypatch.setattr(orchestrator.llm, "chat_stream", fake_chat_stream)
    bus = FakeBus()
    orch = CascadeOrchestrator(bus, _cfg(), "m1", "fast")
    asyncio.run(orch.run(task))
    return bus


_SHORT = ("text", "短い", False)


def test_cascade_truncated_draft_fails_gate_then_repair_adopted(monkeypatch):
    bus = _run_cascade(
        monkeypatch, _TASK,
        draft=("text", _LONG_ANSWER, True),      # 長文・文完結だが打ち切り
        repair=("text", _REPAIR_ANSWER, False),
        skeleton=("text", "骨格の要点", False),
        expand=("text", _EXPAND_ANSWER, False),
        fallback=("text", "フォールバックの最終回答", False))
    # 打ち切りが理由になって下書きは落ち、自己修正が最終回答になる
    assert any("品質ゲート不合格: 生成が長さ上限で打ち切られた" in line
               for line in bus.logs)
    assert bus.completed.get("node-answer") == _REPAIR_ANSWER
    assert bus.finished is None


def test_cascade_truncated_repair_not_adopted(monkeypatch):
    bus = _run_cascade(
        monkeypatch, _TASK,
        draft=_SHORT,
        repair=("text", _LONG_ANSWER, True),     # 自己修正が打ち切り
        skeleton=("text", "骨格の要点", False),
        expand=("text", _EXPAND_ANSWER, False),
        fallback=("text", "フォールバックの最終回答", False))
    # 打ち切られた自己修正は採用されず、骨格→展開へ進む
    assert any("自己修正も不合格: 生成が長さ上限で打ち切られた" in line
               for line in bus.logs)
    assert bus.completed.get("node-answer") == _EXPAND_ANSWER
    assert bus.finished is None


def test_cascade_truncated_expand_falls_back(monkeypatch):
    bus = _run_cascade(
        monkeypatch, _TASK,
        draft=_SHORT,
        repair=_SHORT,
        skeleton=("text", "骨格の要点", False),
        expand=("text", _EXPAND_ANSWER, True),   # 展開が打ち切り
        fallback=("text", "フォールバックの最終回答", False))
    assert any("展開も不合格: 生成が長さ上限で打ち切られた" in line
               for line in bus.logs)
    assert bus.completed.get("node-answer") == "フォールバックの最終回答"
    assert bus.finished is None


def test_cascade_normal_draft_adopted_regression(monkeypatch):
    # 回帰: 打ち切りでない長文の下書きは従来どおり即採用される
    bus = _run_cascade(
        monkeypatch, _TASK,
        draft=("text", _LONG_ANSWER, False),
        repair=_SHORT,
        skeleton=("text", "骨格の要点", False),
        expand=("text", _EXPAND_ANSWER, False),
        fallback=("text", "フォールバックの最終回答", False))
    assert "repair" not in bus.nodes
    assert bus.completed.get("node-answer") == _LONG_ANSWER
    assert bus.finished is None


def test_cascade_empty_fallback_fails_run(monkeypatch):
    # 実績 3991f92d28 の再現: 全段が不調で最終段が本文 0 字を返した
    bus = FakeBus()

    async def fake_chat_stream(cfg, key, messages, **kwargs):
        if key == "fast":
            yield {"content": "短い"}
            yield {"done": True, "meta": {"eval_count": 3}}
            return
        # フォールバック全文生成: 思考だけで本文が返らない
        yield {"thinking": "考え中"}
        yield {"done": True, "meta": {"eval_count": 24}}

    monkeypatch.setattr(orchestrator.llm, "chat_stream", fake_chat_stream)
    orch = CascadeOrchestrator(bus, _cfg(), "m1", "fast")
    # 骨格を飛ばす短答系タスク (下書き・自己修正とも 40 字未満に収まる)
    with pytest.raises(RuntimeError, match="最終回答が空でした"):
        asyncio.run(orch.run("フランスの首都はどこですか"))
    assert bus.finished is not None and "最終回答が空でした" in bus.finished
    assert bus.completed.get("node-answer") is None


# ------------------------------------------------------------ direct

def _run_direct(monkeypatch, spec):
    async def fake_chat_stream(cfg, key, messages, **kwargs):
        for c in _chunks(spec):
            yield c

    monkeypatch.setattr(orchestrator.llm, "chat_stream", fake_chat_stream)
    bus = FakeBus()
    orch = DirectOrchestrator(bus, _cfg(), "m1")
    return bus, orch


def test_direct_empty_answer_fails_run(monkeypatch):
    # 実績の再現: 生成が本文を返さず Run が done になっていた引き
    bus, orch = _run_direct(monkeypatch, ("empty",))
    with pytest.raises(RuntimeError, match="最終回答が空でした"):
        asyncio.run(orch.run("タスク本文"))
    assert bus.finished is not None and "最終回答が空でした" in bus.finished
    assert bus.completed.get("node-answer") is None


def test_direct_normal_answer_unchanged(monkeypatch):
    # 回帰: 通常の回答は従来どおり完了する
    bus, orch = _run_direct(monkeypatch, ("text", _LONG_ANSWER, False))
    asyncio.run(orch.run("タスク本文"))
    assert bus.completed.get("node-answer") == _LONG_ANSWER
    assert bus.finished is None
