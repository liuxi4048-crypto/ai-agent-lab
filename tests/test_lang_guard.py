"""簡体字混入ガード (2026-10-28): 検出スコアの較正と再生成の挙動。

サンプル文は実 Run の最終回答の傾向を再現したもの (きれいな日本語 /
軽微な混入 / 文レベルの混入)。実測較正値は lang_guard の docstring 参照。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lang_guard
import orchestrator
from orchestrator import DirectOrchestrator, _guard_lang

CLEAN_JA = (
    "分数の割り算は、割る数の逆数を掛けることで計算できます。たとえば "
    "3/4 ÷ 2/5 は、2/5 の逆数 5/2 を掛けて 15/8 になります。これは、"
    "割り算が「いくつ分あるか」を数える操作だからです。具体的には、"
    "まず単位分数で考えると分かりやすくなります。"
)

MILD = CLEAN_JA + "つまり、言い換えると，也就是、逆数を掛ける操作です。"

CONTAMINATED = (
    "分数の割り算の意味を理解しましょう。除法的意思是「いくつ分あるか」です。"
    "因为除以一个分数，就相当于乘以它的逆数，所以计算变得很简单。"
    "比如 3/4 ÷ 2/5，我们可以把它变成乘法，然后约分，最后得到答案。"
    "这个方法非常实用，学生们需要掌握。"
)


def test_detector_separates_clean_mild_and_contaminated():
    assert lang_guard.contamination_score(CLEAN_JA) < lang_guard.CONTAMINATION_THRESHOLD
    assert lang_guard.contamination_score(MILD) < lang_guard.CONTAMINATION_THRESHOLD
    assert lang_guard.contamination_score(CONTAMINATED) >= lang_guard.CONTAMINATION_THRESHOLD
    assert not lang_guard.is_contaminated(CLEAN_JA)
    assert lang_guard.is_contaminated(CONTAMINATED)


def test_detector_handles_empty():
    assert lang_guard.contamination_score("") == 0
    assert not lang_guard.is_contaminated("")


class FakeBus:
    def __init__(self):
        self.logs = []

    def emit_log(self, node_id, line):
        self.logs.append(line)


def _msgs():
    return [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]


def test_guard_retries_once_and_adopts_cleaner():
    calls = []

    async def gen(messages, early_abort):
        calls.append((messages, early_abort))
        return CONTAMINATED if len(calls) == 1 else CLEAN_JA

    bus = FakeBus()
    out = asyncio.run(_guard_lang(bus, "n1", _msgs(), gen))
    assert out == CLEAN_JA
    assert len(calls) == 2
    # 1 回目は早期打ち切り付き、再生成は打ち切りなし
    assert calls[0][1] is True and calls[1][1] is False
    # 再生成では system に強い指示が追加され、元の messages は壊れない
    assert "前回の出力には中国語" in calls[1][0][0]["content"]
    assert calls[0][0][0]["content"] == "SYS"
    assert any("言語ガード" in line for line in bus.logs)


def test_guard_no_retry_when_clean():
    calls = []

    async def gen(messages, early_abort):
        calls.append(messages)
        return CLEAN_JA

    out = asyncio.run(_guard_lang(FakeBus(), "n1", _msgs(), gen))
    assert out == CLEAN_JA and len(calls) == 1


def test_guard_keeps_original_when_retry_is_worse():
    calls = []

    async def gen(messages, early_abort):
        calls.append(messages)
        return CONTAMINATED if len(calls) == 1 else CONTAMINATED + CONTAMINATED

    out = asyncio.run(_guard_lang(FakeBus(), "n1", _msgs(), gen))
    assert out == CONTAMINATED and len(calls) == 2


def test_guard_abort_goes_straight_to_retry():
    flags = []

    async def gen(messages, early_abort):
        flags.append(early_abort)
        if early_abort:
            raise orchestrator._LangAborted()
        return CLEAN_JA

    bus = FakeBus()
    out = asyncio.run(_guard_lang(bus, "n1", _msgs(), gen))
    assert out == CLEAN_JA
    assert flags == [True, False]
    assert any("打ち切り" in line for line in bus.logs)


def test_partial_detector_needs_length_and_density():
    long_contaminated = CONTAMINATED * 4          # 400 字超・高密度
    assert lang_guard.partial_is_contaminated(long_contaminated)
    assert not lang_guard.partial_is_contaminated(CONTAMINATED)   # 短いと判定しない
    assert not lang_guard.partial_is_contaminated(CLEAN_JA * 4)   # きれいな長文
    assert not lang_guard.partial_is_contaminated(MILD * 4)       # 軽微な混入の長文


class StreamBus:
    def __init__(self):
        self.logs = []

    def set_prompt(self, node_id, prompt):
        pass

    def set_model(self, node_id, key, tag):
        pass

    def set_status(self, node_id, status):
        pass

    def think_progress(self, node_id, text):
        pass

    def token_progress(self, node_id, text):
        pass

    def set_tokens(self, node_id, n):
        pass

    def emit_log(self, node_id, line):
        self.logs.append(line)


def test_stream_llm_early_abort(monkeypatch):
    import llm

    consumed = []

    async def fake_chat_stream(cfg, key, messages, **kwargs):
        for i in range(50):
            consumed.append(i)
            yield {"content": CONTAMINATED[:60], "done": False}

    monkeypatch.setattr(llm, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(llm, "resolve", lambda cfg, key: {"tag": "x"})

    async def main():
        await orchestrator._stream_llm(
            StreamBus(), "n1", _msgs(), {}, "deepseek_strata",
            early_abort=lang_guard.partial_is_contaminated)

    try:
        asyncio.run(main())
        raise AssertionError("_LangAborted が送出されなかった")
    except orchestrator._LangAborted:
        pass
    # 400 字 (7 チャンク目) に達した時点で打ち切られ、全量は消費しない
    assert len(consumed) < 50


def test_direct_orchestrator_applies_guard(monkeypatch):
    calls = []

    async def fake_stream(bus, node_id, messages, cfg, key, **kwargs):
        calls.append(messages)
        return CONTAMINATED if len(calls) == 1 else CLEAN_JA

    monkeypatch.setattr(orchestrator, "_stream_llm", fake_stream)

    class Bus:
        def __init__(self):
            self.nodes = {}
            self._seq = 0
            self.finished = False
            self.error = None

        def reset(self):
            pass

        def create_node(self, kind, title, detail, parent_id=None):
            self._seq += 1
            node_id = f"n{self._seq}"
            self.nodes[node_id] = {"kind": kind}
            return node_id

        def set_status(self, node_id, status):
            pass

        def emit_log(self, node_id, line):
            pass

        def complete(self, node_id, output=None, error=None):
            self.nodes[node_id]["output"] = output

        def cancel_all(self, reason):
            pass

        def run_finished(self, error=None):
            self.finished = True
            self.error = error

    bus = Bus()
    orc = DirectOrchestrator(bus, cfg={}, model="deepseek_strata")
    asyncio.run(orc.run("task"))
    answers = [n for n in bus.nodes.values() if n["kind"] == "answer"]
    assert len(calls) == 2                       # 混入検出で 1 回だけ再生成
    assert answers[0]["output"] == CLEAN_JA      # きれいな方を採用
    assert bus.finished and bus.error is None
