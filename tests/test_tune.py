import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm
import tune


def test_payload_options_override():
    cfg = {"models": {"m": {"tag": "t:1", "options": {"temperature": 0.7}}}}
    info = llm.resolve(cfg, "m")
    p = llm._payload(info, [], None, None, None, False, True,
                     options_override={"num_batch": 2048})
    assert p["options"]["num_batch"] == 2048
    assert p["options"]["temperature"] == 0.7        # 既存の公式値は残る
    p2 = llm._payload(info, [], None, None, None, False, True)
    assert "num_batch" not in p2["options"]          # 未指定なら送らない


def test_choose_threshold():
    base = {"num_batch": None, "wall_ms": 1000.0}
    # 5%未満の改善は誤差として採らない
    assert tune.choose([base, {"num_batch": 1024, "wall_ms": 960.0}]) is None
    # 5%以上速い候補のうち最速を採る
    assert tune.choose([base, {"num_batch": 1024, "wall_ms": 900.0},
                        {"num_batch": 2048, "wall_ms": 800.0}]) == 2048
    # 基準が測れていない場合は決めない
    assert tune.choose([{"num_batch": None, "wall_ms": None},
                        {"num_batch": 1024, "wall_ms": 100.0}]) is None
    # 失敗した候補(None)は無視する
    assert tune.choose([base, {"num_batch": 4096, "wall_ms": None}]) is None


def _fake_chat(speeds):
    """num_batch ごとに決まった _timing を返す chat の偽物。"""
    async def chat(cfg, key, messages, options_override=None, **kw):
        nb = (options_override or {}).get("num_batch")
        wall, tps, pe = speeds[nb]
        return {"role": "assistant", "content": "ok",
                "_timing": {"wall_ms": wall, "tok_per_s": tps,
                            "prompt_eval_ms": pe, "prompt_tokens": 1000,
                            "load_ms": 0}}
    return chat


def test_run_sweep_picks_fastest():
    speeds = {None: (1000.0, 40.0, 500.0), 1024: (900.0, 44.0, 600.0),
              2048: (700.0, 50.0, 900.0), 4096: (850.0, 46.0, 700.0)}
    results, adopted = asyncio.run(tune.run_sweep(
        {"models": {}}, "m", chat_fn=_fake_chat(speeds), rounds=1,
        emit=lambda *a: None))
    assert adopted == 2048
    assert [r["num_batch"] for r in results] == [None, 1024, 2048, 4096]


SAMPLE_YAML = """models:
  coder:
    tag: "qwen3-coder:30b"
    placement: vram
    options: {temperature: 0.7, top_p: 0.8}  # 公式推奨
  deep:
    tag: "big:1"
    placement: hybrid
    options: {temperature: 0.6, num_batch: 512}
default: coder
"""


def test_apply_option_inserts_and_replaces():
    out, changed = tune.apply_option(SAMPLE_YAML, "coder", "num_batch", 2048)
    assert changed
    assert "options: {temperature: 0.7, top_p: 0.8, num_batch: 2048}  # 公式推奨" in out
    assert "num_batch: 512" in out                     # 他モデルは不変
    out2, changed2 = tune.apply_option(SAMPLE_YAML, "deep", "num_batch", 1024)
    assert changed2 and "num_batch: 1024}" in out2     # 既存値は置換
    _, changed3 = tune.apply_option(SAMPLE_YAML, "missing", "num_batch", 1)
    assert not changed3                                # 対象が無ければ無変更


def test_apply_to_models_yaml_roundtrip(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(SAMPLE_YAML, encoding="utf-8")
    ok, msg = tune.apply_to_models_yaml(p, "coder", {"num_batch": 2048},
                                        stamp="test")
    assert ok
    import yaml
    saved = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert saved["models"]["coder"]["options"]["num_batch"] == 2048
    assert (tmp_path / "models.yaml.bak-test").exists()


def test_num_thread_candidates():
    cands = tune.num_thread_candidates()
    assert cands[0] is None and len(cands) >= 2
    assert len(set(cands)) == len(cands)


def test_run_sweep_survives_candidate_failure():
    async def flaky(cfg, key, messages, options_override=None, **kw):
        nb = (options_override or {}).get("num_batch")
        if nb == 4096:
            raise RuntimeError("boom")
        wall = 500.0 if nb else 1000.0
        return {"role": "assistant", "content": "ok",
                "_timing": {"wall_ms": wall, "tok_per_s": 50.0,
                            "prompt_eval_ms": 100.0, "prompt_tokens": 100,
                            "load_ms": 0}}
    results, adopted = asyncio.run(tune.run_sweep(
        {"models": {}}, "m", chat_fn=flaky, rounds=1, emit=lambda *a: None))
    assert adopted == 1024                          # 失敗候補を除いて決まる
    assert results[-1]["wall_ms"] is None


def test_measure_aggregates_per_task():
    # 課題ごとに集計し、wall は課題別中央値の合計(混ぜた中央値ではない)
    async def chat(cfg, key, messages, options_override=None, **kw):
        content = messages[0]["content"]
        if "フィボナッチ" in content:
            wall, tps, pe, pt = 300.0, 60.0, 10.0, 50
        else:
            wall, tps, pe, pt = 700.0, 10.0, 2000.0, 2000
        return {"role": "assistant", "content": "ok",
                "_timing": {"wall_ms": wall, "tok_per_s": tps,
                            "prompt_eval_ms": pe, "prompt_tokens": pt,
                            "load_ms": 0}}
    m = asyncio.run(tune._measure(chat, {}, "m", None, rounds=2))
    assert m["wall_ms"] == 1000.0            # 700 + 300(課題別の合計)
    assert m["decode_tps"] == 60.0           # 生成課題のみ
    assert m["prompt_tps"] == 1000.0         # 読込課題のみ(2000tok / 2.0s)


def test_num_gpu_candidates():
    assert tune.num_gpu_candidates(None) == [None]
    assert tune.num_gpu_candidates(3) == [None]
    c = tune.num_gpu_candidates(64)
    assert c == [None, 16, 32, 48]
    assert tune.num_gpu_candidates(40) == [None, 10, 20, 30]


def test_split_apply_values():
    apply_values, proposals = tune.split_apply_values(
        {"num_batch": 2048, "num_ctx": 8192, "num_gpu": 32})
    assert apply_values == {"num_batch": 2048, "num_gpu": 32}
    assert proposals == {"num_ctx": 8192}
