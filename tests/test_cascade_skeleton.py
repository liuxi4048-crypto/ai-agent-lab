"""カスケード骨格段の空チェック (2026-11-01) の検証。

根拠: Run ストア実績 (cascade 30 件中、骨格に到達した 8 件) で、本文 0 字の
空骨格が 3 件あり、いずれも展開が情報ゼロの骨格から生成してゲート不合格に
なっていた (高性能モデルが think=False 明示でもネイティブ推論で思考だけを
生成し、上限到達で本文 0 字になる引き)。対策として、骨格が空 (0 字) の
ときだけ展開を飛ばしてフォールバック全文生成へ直行する。短いが中身の
ある骨格は正当でありうるため、長さでの一律判定はしないことを併せて固定
する。LLM ストリームを fake に差し替えて検証する。
"""
import asyncio

import orchestrator
from orchestrator import CascadeOrchestrator


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


# 展開がゲート (_draft_gate) を通過できる長文の日本語回答 (80 字以上・文完結)
_LONG_ANSWER = ("これは展開によって書かれた最終回答です。"
                "骨格の要点をすべて含み、具体例も挙げて説明しています。"
                "読み手が迷わないように、手順を順番に整理して示します。"
                "最後に注意点として、途中の計算を省略せず一行ずつ確認する"
                "ことが大切であると付け加えておきます。")


def _is_expand_call(messages):
    return any(orchestrator.CASCADE_EXPAND_SYSTEM == (m.get("content") or "")
               for m in messages if m.get("role") == "system")


def _run_cascade(monkeypatch, skeleton_chunks, expand_text=_LONG_ANSWER):
    calls = []

    async def fake_chat_stream(cfg, key, messages, **kwargs):
        calls.append({"key": key, "messages": messages, "kwargs": kwargs})
        if key == "fast":
            if _is_expand_call(messages):
                yield {"content": expand_text}
                yield {"done": True, "meta": {"eval_count": 50}}
            else:
                # 下書き・自己修正は短すぎてゲート不合格になる
                yield {"content": "短い"}
                yield {"done": True, "meta": {"eval_count": 3}}
            return
        if kwargs.get("think") is False:
            # 骨格 (高性能モデル・think=False 明示)
            for c in skeleton_chunks:
                yield c
            yield {"done": True, "meta": {"eval_count": 100}}
            return
        # フォールバック全文生成 (第 4 段)
        yield {"content": "フォールバックの最終回答"}
        yield {"done": True, "meta": {"eval_count": 42}}

    monkeypatch.setattr(orchestrator.llm, "chat_stream", fake_chat_stream)
    bus = FakeBus()
    orch = CascadeOrchestrator(bus, _cfg(), "m1", "fast")
    asyncio.run(orch.run("分数の割り算の仕組みを説明してください"))
    return bus, calls


def test_empty_skeleton_skips_expand(monkeypatch):
    # 実績の再現: 思考だけが生成され、本文は 0 字のまま上限到達する引き
    bus, calls = _run_cascade(monkeypatch, [{"thinking": "あ" * 300}])
    # 骨格ノードは作られるが、展開は呼ばれずフォールバックへ直行する
    assert "skeleton" in bus.nodes
    assert "expand" not in bus.nodes
    assert not any(_is_expand_call(c["messages"]) for c in calls)
    assert bus.completed.get("node-skeleton") == ""
    assert any("骨格が空のため展開を省略" in line for line in bus.logs)
    assert bus.completed.get("node-answer") == "フォールバックの最終回答"
    assert bus.finished is None


def test_blank_only_skeleton_skips_expand(monkeypatch):
    # 空白だけの骨格も空として扱う
    bus, calls = _run_cascade(monkeypatch, [{"content": "  \n  "}])
    assert "expand" not in bus.nodes
    assert not any(_is_expand_call(c["messages"]) for c in calls)
    assert bus.completed.get("node-answer") == "フォールバックの最終回答"
    assert bus.finished is None


def test_nonempty_skeleton_still_expands(monkeypatch):
    # 回帰: 通常の骨格では従来どおり展開が走り、通過すればそれが最終回答
    bus, calls = _run_cascade(monkeypatch, [{"content": "骨格の要点"}])
    assert "expand" in bus.nodes
    assert any(_is_expand_call(c["messages"]) for c in calls)
    assert bus.completed.get("node-answer") == _LONG_ANSWER
    assert bus.finished is None


def test_short_skeleton_still_expands(monkeypatch):
    # 実質空 (短いが中身のある骨格) は対象外: 長さで一律に飛ばさない
    bus, calls = _run_cascade(monkeypatch, [{"content": "要点のみ。"}])
    assert "expand" in bus.nodes
    assert any(_is_expand_call(c["messages"]) for c in calls)
    assert not any("骨格が空" in line for line in bus.logs)
    assert bus.completed.get("node-answer") == _LONG_ANSWER
    assert bus.finished is None
