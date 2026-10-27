"""自動チューニング: Ollama の実行時オプション候補を実測で比較し、最適値を提案する。

使い方:
    python tune.py --model coder             # 候補を実測して提案を表示
    python tune.py --model coder --apply     # 採用候補を models.yaml へ反映(バックアップ付き)
    python tune.py --model coder --rounds 3  # 計測回数を増やす(既定2)

設計:
- 対象は num_batch(プロンプト処理のバッチ幅。大きいほど prefill が速くなる
  一方で VRAM を多く使う)と、hybrid(RAM併用)モデルでは num_thread
  (CPU 側の計算スレッド数)と num_gpu(GPU へ載せる層数)。--ctx 指定時は
  num_ctx も測る(文脈長とのトレードオフのため自動反映はしない)。
  モデルの既定(None)を基準に候補を1項目ずつ試す(座標降下)。
- 計測は固定課題(長文 prefill + 短文生成。num_batch では約8Kの長文も)を
  候補ごとにウォームアップ1回+本計測 rounds 回行い、課題ごとに中央値を
  取って合計で比べる。モデル再ロード(load_ms が大きい回)は計測から除く。
  結果は候補ごとに逐次保存され、途中で落ちても計測は全損しない。
- 採否は「2課題の合計所要時間が基準より 5% 以上短い候補のうち最速のもの」。
  5% 未満の差は計測誤差として基準(None=models.yaml のまま)を維持する
  (Strata の `strata launch --tune` と同じ考え方)。
- 既定では models.yaml は書き換えず、結果 JSON を tune_results/ に保存して
  書くべき断片を表示するだけ。--apply のときだけ、コメントを保ったまま
  options 行へ外科的に挿入し、検証(YAML再読込で値を確認)を通してから
  バックアップ(models.yaml.bak-<時刻>)を残して書き込む。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import yaml

import llm

BASE = Path(__file__).resolve().parent
RESULTS_DIR = BASE / "tune_results"

NUM_BATCH_CANDIDATES = [None, 1024, 2048, 4096]   # None = モデル既定(基準)
ADOPT_THRESHOLD = 0.05                            # 基準より5%以上速くない候補は採らない

_PARA = ("ローカルLLMは、データを外部に送らずに手元のコンピュータで推論できる点が"
         "大きな利点です。一方で、モデルの読み込みやメモリ配置の工夫が性能を左右します。"
         "特に大きなモデルでは、グラフィックスメモリに載せきれない部分をメインメモリや"
         "SSDから読みながら計算するため、読み込みの幅やキャッシュの使い方が速度に直結します。")
PREFILL_TEXT = _PARA * 30                          # 長文プロンプト(概算1.5K〜2Kトークン)


def num_thread_candidates():
    """num_thread の候補: 既定 / 論理コア数の半分 / 論理コア数。"""
    logical = os.cpu_count() or 4
    cands = [None]
    for v in (max(1, logical // 2), logical):
        if v not in cands:
            cands.append(v)
    return cands


def _median(xs):
    return statistics.median(xs) if xs else None


def num_gpu_candidates(total_layers):
    """num_gpu(GPUへ載せる層数)の候補: 既定 / 全層の25% / 50% / 75%。

    hybrid の GPU/CPU 層配分は速度を決める最大級のノブだが、Ollama の自動
    配置任せで一度も測られていなかった(2026-10-27監査)。全層数未満に限る
    (全層GPUは vram 配置の話で hybrid の測定対象ではない)。
    """
    if not total_layers or total_layers < 4:
        return [None]
    cands = [None]
    for frac in (0.25, 0.5, 0.75):
        v = max(1, round(total_layers * frac))
        if v < total_layers and v not in cands:
            cands.append(v)
    return cands


async def _block_count(tag):
    """/api/show の model_info から総層数(*.block_count)を取る。取れなければ None。"""
    client = llm._client_for("ollama")
    try:
        r = await client.post(f"{llm.OLLAMA_BASE}/api/show",
                              json={"model": tag}, timeout=30)
        info = (r.json() or {}).get("model_info") or {}
    except Exception:
        return None
    for k, v in info.items():
        if k.endswith(".block_count") and isinstance(v, int):
            return v
    return None


# num_ctx は「速さ」と「扱える文脈の長さ」のトレードオフなので、--apply の
# 自動反映からは外し、提案表示のみにする。
PROPOSAL_ONLY = {"num_ctx"}


def split_apply_values(adopted):
    """採用候補を (自動反映してよい値, 提案のみの値) に分ける。"""
    apply_values = {k: v for k, v in adopted.items() if k not in PROPOSAL_ONLY}
    proposals = {k: v for k, v in adopted.items() if k in PROPOSAL_ONLY}
    return apply_values, proposals


async def _measure(chat_fn, cfg, key, options_override, rounds, long_task=False):
    """固定課題の計測。戻り値: {wall_ms, decode_tps, prompt_tps}。

    集計は課題ごとに行う(2026-10-27監査で修正: 以前は性質の違う課題の
    wall を混ぜて中央値を取り「合計」と表示していた)。wall_ms は課題別の
    中央値の合計、decode_tps は生成課題のみ、prompt_tps は読込課題のみ。
    ウォームアップ1回を捨て、load_ms が 1000ms を超えた回(モデル再ロードが
    混入した回)も捨てる。どちらかの課題が全滅した候補は None。
    long_task=True では約8Kトークンの長文読込も課題に加える(num_batch は
    プロンプト長で効きと危険が変わるため)。
    """
    tasks = [
        ("prefill", PREFILL_TEXT + "\n\n上の文章を3行で要約してください。", 48),
        ("decode", "Pythonでフィボナッチ数列の最初の20項を返す関数を書いてください。コードだけを出力してください。", 128),
    ]
    if long_task:
        tasks.append(("prefill_long", (PREFILL_TEXT * 4) +
                      "\n\n上の文章を3行で要約してください。", 32))
    total_wall = 0.0
    decode_tps = prompt_tps = None
    for kind, prompt, n_predict in tasks:
        walls, tps_list, prompt_list = [], [], []
        for r in range(rounds + 1):
            msg = await chat_fn(cfg, key, [{"role": "user", "content": prompt}],
                                options_override=options_override, retries=1,
                                num_predict=n_predict)
            meta = llm.strip_meta(msg)
            timing = meta.get("_timing") or {}
            if r == 0 or (timing.get("load_ms") or 0) > 1000:
                continue                       # ウォームアップ / 再ロード混入は捨てる
            if timing.get("wall_ms"):
                walls.append(timing["wall_ms"])
            if timing.get("tok_per_s"):
                tps_list.append(timing["tok_per_s"])
            pe_ms = timing.get("prompt_eval_ms")
            if pe_ms and timing.get("prompt_tokens"):
                prompt_list.append(timing["prompt_tokens"] / (pe_ms / 1000))
        if not walls:
            return None
        total_wall += _median(walls)
        if kind == "decode":
            decode_tps = _median(tps_list)
        elif kind == "prefill":
            prompt_tps = _median(prompt_list)
    return {"wall_ms": total_wall, "decode_tps": decode_tps,
            "prompt_tps": prompt_tps}


async def run_sweep(cfg, key, candidates=NUM_BATCH_CANDIDATES, rounds=2,
                    chat_fn=None, emit=print, option="num_batch", base_override=None,
                    long_task=False, on_result=None):
    """1項目の候補を順に計測し、(結果リスト, 採用候補) を返す。採否判定は choose()。

    base_override は、先に採用が決まった項目の固定値(例: num_batch の採用値の
    上で num_thread を測るための座標降下)。on_result は候補ごとに呼ばれ、
    途中経過の保存(長時間スイープが落ちても計測を全損しない)に使う。
    """
    chat_fn = chat_fn or llm.chat
    results = []
    for cand in candidates:
        override = dict(base_override or {})
        if cand is not None:
            override[option] = cand
        label = "既定" if cand is None else f"{option}={cand}"
        emit(f"[tune] {label} を計測中...")
        try:
            m = await _measure(chat_fn, cfg, key, override or None, rounds,
                               long_task=long_task)
        except Exception as e:                 # 候補が大きすぎて失敗しても全体は続ける
            emit(f"[tune] {label}: 失敗({type(e).__name__})")
            m = None
        results.append({option: cand, "value": cand, **(m or {"wall_ms": None})})
        if m:
            emit(f"       合計 {m['wall_ms']:.0f}ms / 生成 "
                 f"{(m['decode_tps'] or 0):.1f} tok/s / 読込 "
                 f"{(m['prompt_tps'] or 0):.0f} tok/s")
        if on_result:
            on_result(results)
    return results, choose(results)


def _value_of(r):
    return r.get("value", r.get("num_batch"))


def choose(results):
    """計測結果から採用候補を決める。基準(既定)より5%以上速い最速候補のみ。

    戻り値: 採用する候補値(無ければ None)。
    """
    base = next((r for r in results if _value_of(r) is None), None)
    if not base or not base.get("wall_ms"):
        return None
    limit = base["wall_ms"] * (1 - ADOPT_THRESHOLD)
    better = [r for r in results
              if _value_of(r) is not None and r.get("wall_ms")
              and r["wall_ms"] <= limit]
    if not better:
        return None
    return _value_of(min(better, key=lambda r: r["wall_ms"]))


def suggestion(key, name, value):
    """models.yaml へ書くべき断片(--apply でない場合の表示用)。"""
    return (f"  # {key} の options: {{...}} の中に追加(tune.py の実測で採用):\n"
            f"  #   , {name}: {value}")


def apply_option(text, key, name, value):
    """models.yaml のテキストで key の options 行へ name: value を外科的に入れる。

    コメントや書式は一切変えず、対象モデルの options: {...} の中だけを触る。
    既に name があれば値を置換する。戻り値: (新しいテキスト, 変更できたか)。
    """
    lines = text.splitlines(keepends=True)
    in_block = False
    for i, line in enumerate(lines):
        m = re.match(r"^  ([\w\-]+):\s*(#.*)?$", line)
        if m:
            in_block = (m.group(1) == key)
            continue
        if not in_block:
            continue
        mo = re.match(r"^(\s*)options:\s*\{(.*)\}(.*)$", line.rstrip("\n"))
        if mo:
            inner, rest = mo.group(2), mo.group(3)
            if re.search(rf"\b{re.escape(name)}\s*:", inner):
                inner = re.sub(rf"\b{re.escape(name)}\s*:\s*[^,}}]+",
                               f"{name}: {value}", inner)
            else:
                inner = inner.rstrip() + f", {name}: {value}"
            lines[i] = f"{mo.group(1)}options: {{{inner}}}{rest}\n"
            return "".join(lines), True
    return text, False


def apply_to_models_yaml(path, key, values, stamp=None):
    """採用値を models.yaml へ反映する。検証を通らなければ書き込まない。

    戻り値: (成功か, メッセージ)。成功時はバックアップを残す。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    new = text
    for name, value in values.items():
        new, changed = apply_option(new, key, name, value)
        if not changed:
            return False, f"{key} の options 行が見つからず {name} を反映できませんでした"
    got = ((yaml.safe_load(new) or {}).get("models", {}).get(key, {})
           .get("options") or {})
    for name, value in values.items():
        if got.get(name) != value:
            return False, "反映後の検証に失敗しました(書き込んでいません)"
    backup = path.with_name(path.name + f".bak-{stamp or time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    return True, f"反映しました(バックアップ: {backup.name})"


async def _amain(args):
    cfg = llm.load_config()
    info = llm.resolve(cfg, args.model)
    if info.get("backend") != "ollama":
        print(f"[tune] {args.model} は Ollama バックエンドではないため対象外です")
        return 1
    if not await llm.is_alive():
        print("[tune] Ollama が起動していません(ollama serve を実行してください)")
        return 1
    print(f"[tune] model={args.model} ({info['tag']}) を実測します(数分かかります)")
    # think はモデル既定のまま測る(llm.chat が models.yaml の think を使う)。
    # 比較可能性のため、結果にはその設定を記録する(2026-10-27監査)。
    report = {"model": args.model, "tag": info["tag"],
              "think": info.get("think"), "sweeps": {}, "adopted": {}}

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"tune-{args.model}-{stamp}.json"

    def save_partial(_results=None):
        # 候補ごとの逐次保存: 長時間スイープが途中で落ちても計測を全損しない
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                       encoding="utf-8")

    fixed = {}
    results, adopted = await run_sweep(cfg, args.model, rounds=args.rounds,
                                       long_task=True, on_result=save_partial)
    report["sweeps"]["num_batch"] = results
    save_partial()
    if adopted is not None:
        report["adopted"]["num_batch"] = adopted
        fixed["num_batch"] = adopted

    if info.get("placement") == "hybrid":
        # RAM併用モデルは専門家の大半を CPU で計算するため num_thread も効く。
        # さらに層の GPU/CPU 配分(num_gpu)は最大級のノブ。どちらも先行する
        # 採用値を固定した上で測る(座標降下)。
        results_t, adopted_t = await run_sweep(
            cfg, args.model, candidates=num_thread_candidates(), rounds=args.rounds,
            option="num_thread", base_override=fixed or None,
            on_result=save_partial)
        report["sweeps"]["num_thread"] = results_t
        save_partial()
        if adopted_t is not None:
            report["adopted"]["num_thread"] = adopted_t
            fixed["num_thread"] = adopted_t

        layers = await _block_count(info["tag"])
        gpu_cands = num_gpu_candidates(layers)
        if len(gpu_cands) > 1:
            results_g, adopted_g = await run_sweep(
                cfg, args.model, candidates=gpu_cands, rounds=args.rounds,
                option="num_gpu", base_override=fixed or None,
                on_result=save_partial)
            report["sweeps"]["num_gpu"] = results_g
            save_partial()
            if adopted_g is not None:
                report["adopted"]["num_gpu"] = adopted_g
                fixed["num_gpu"] = adopted_g
        else:
            print("[tune] num_gpu: 層数が取得できないためスキップしました")

    if args.ctx:
        # num_ctx は文脈長とのトレードオフがあるため、測るのは --ctx 指定時の
        # み。採用されても自動反映はせず提案表示のみ(PROPOSAL_ONLY)。
        ctx_cands = [None] + [c for c in (8192, 32768) if c != info["num_ctx"]]
        results_c, adopted_c = await run_sweep(
            cfg, args.model, candidates=ctx_cands, rounds=args.rounds,
            option="num_ctx", base_override=fixed or None, on_result=save_partial)
        report["sweeps"]["num_ctx"] = results_c
        save_partial()
        if adopted_c is not None:
            report["adopted"]["num_ctx"] = adopted_c

    save_partial()
    print(f"\n[tune] 結果を保存: {out}")
    adopted_map = report["adopted"]
    if not adopted_map:
        print("[tune] 既定より5%以上速い候補はありませんでした。models.yaml の変更は不要です")
        return 0
    for name, value in adopted_map.items():
        print(f"[tune] 採用候補: {name}={value}")
    apply_values, proposals = split_apply_values(adopted_map)
    for name, value in proposals.items():
        print(f"[tune] {name} は文脈長とのトレードオフのため自動反映しません")
        print(suggestion(args.model, name, value))
    if not apply_values:
        return 0
    if args.apply:
        ok, msg = apply_to_models_yaml(llm.CONFIG_PATH, args.model, apply_values)
        print(f"[tune] {msg}")
        return 0 if ok else 1
    for name, value in apply_values.items():
        print(suggestion(args.model, name, value))
    print("[tune] 反映するには --apply を付けて実行してください")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Ollama 実行時オプションの自動チューニング")
    ap.add_argument("--model", required=True, help="models.yaml のキー")
    ap.add_argument("--rounds", type=int, default=2, help="候補ごとの本計測回数(既定2)")
    ap.add_argument("--apply", action="store_true",
                    help="採用候補を models.yaml へ反映する(バックアップ付き)")
    ap.add_argument("--ctx", action="store_true",
                    help="num_ctx も測る(採用されても自動反映せず提案のみ)")
    args = ap.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
