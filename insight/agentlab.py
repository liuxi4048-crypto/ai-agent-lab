"""ai-agent-lab の Run 記録(runs/<id>.json)を、インサイト画面用のトレースに変換する。

記録にあるのは各ノード(エージェント)の開始・終了時刻、トークン数、ログ行だけで、
ログ行には時刻が無い。そのためノード内の出来事は「順序」だけを扱い、時刻は推定しない。
本文(プロンプト・出力・思考)は字数だけを残し、内容は保存しない。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
ID_RE = re.compile(r"^[0-9a-f]{6,32}$")

ITER_RE = re.compile(r"=== iter (\d+)/(\d+)\s+phase=(\w+)")
TOOL_RE = re.compile(r"^\s*-> (\w+)\((.*)", re.S)
COMPACT_RE = re.compile(r"\[履歴圧縮\].*?(\d+)/(\d+) tokens")
EXIT_RE = re.compile(r"^\s*exit=(-?\d+)")
AUTH_RE = re.compile(r"(?i)\b(bearer|basic)\s+\S+")
SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)(\"?\s*[=:]\s*\"?)([^\s\",}]+)")

# 親→子の関係が何を意味するか(画面の矢印の説明に使う)
EDGE_MEANING = {
    ("task", "planner"): "タスク全体を計画役に渡し、小さな作業に分けてもらう。",
    ("task", "subagent"): "分けた作業の1つを子エージェントに渡す。子は自分の文脈だけで作業する。",
    ("task", "aggregator"): "子エージェントの結果を集約役に渡し、1つの答えにまとめる。",
    ("task", "answer"): "最終的な答えを利用者に返す。",
    ("task", "coder"): "目標をコーディングエージェントに渡す。ツールを使って作り、確かめ、直す。",
    ("task", "round"): "批評ループの1往復を始める。",
    ("round", "author"): "作成役が案を書く(または前回の批評を受けて直す)。",
    ("round", "reviewer"): "別系統のモデルが案を批評する。",
    ("task", "merger"): "並列に作られた部品を1つにまとめる。",
}


def _clip(s: str, n: int) -> str:
    s = SECRET_RE.sub(r"\1\2<REDACTED>", AUTH_RE.sub(r"\1 <REDACTED>", s))
    return s if len(s) <= n else s[:n] + "…"


# ファイルの (mtime, size) を鍵にしたメモ化。インサイト画面は開くたびに
# 最大80件の Run 記録(snapshot 全体)を全量パースしていた(2026-10-27監査)。
_file_cache: dict = {}


def _cached(path: Path, kind: str, build):
    # kind は用途(row=一覧の1行 / trace=詳細)。同じファイルでも返す形が違う
    # ため、鍵を混ぜると形の違う結果が返ってしまう。
    cache_key = f"{kind}:{path}"
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return build()
    hit = _file_cache.get(cache_key)
    if hit and hit[0] == key:
        return hit[1]
    value = build()
    _file_cache[cache_key] = (key, value)
    return value


def _run_row(f: Path) -> dict | None:
    def build():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        nodes = (d.get("snapshot") or {}).get("nodes") or []
        return {"id": d.get("id", f.stem), "task": _clip(d.get("task") or "", 80),
                "mode": d.get("mode"), "status": d.get("status"),
                "created_at": d.get("created_at"), "tokens": d.get("tokens"),
                "nodes": len(nodes)}
    return _cached(f, "row", build)


def list_runs(limit: int = 80) -> list[dict]:
    rows = []
    for f in sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        row = _run_row(f)
        if row:
            rows.append(row)
    return rows


def _parse_log(lines: list[str]) -> dict:
    """ログ行から、反復・ツール呼び出しとその結果・履歴圧縮・停止理由を順序つきで取り出す。"""
    events, tools, compactions = [], [], []
    iters, max_iter, stop = 0, None, None
    for raw in lines:
        line = raw if isinstance(raw, str) else str(raw)
        if m := ITER_RE.search(line):
            iters, max_iter = int(m.group(1)), int(m.group(2))
            events.append({"type": "iter", "label": f"反復 {m.group(1)}/{m.group(2)}({m.group(3)})"})
        elif m := TOOL_RE.match(line):
            t = {"name": m.group(1), "args": _clip(m.group(2).rstrip(")"), 140), "ok": None, "result": ""}
            tools.append(t)
            events.append({"type": "tool", "label": f"ツール {t['name']}", "tool": len(tools) - 1})
        elif m := COMPACT_RE.search(line):
            compactions.append({"used": int(m.group(1)), "limit": int(m.group(2))})
            events.append({"type": "compact", "label": f"履歴圧縮(実測 {m.group(1)}/{m.group(2)} トークン)"})
        elif "[MAX_ITER 到達]" in line:
            stop = "max_iter"
            events.append({"type": "stop", "label": "反復の上限に到達して停止"})
        elif "=== 完了 ===" in line:
            stop = stop or "done"
            events.append({"type": "stop", "label": "完了"})
        elif tools and tools[-1]["ok"] is None:
            # ツールの直後の行が結果。成否は exit コードとエラー表記で判断する
            if m := EXIT_RE.match(line):
                tools[-1]["ok"] = m.group(1) == "0"
                tools[-1]["result"] = f"exit={m.group(1)}"
            elif any(w in line for w in ("エラー", "Error", "見つかりません", "却下", "拒否")):
                tools[-1]["ok"] = False
                tools[-1]["result"] = _clip(line.strip(), 120)
            elif line.strip():
                tools[-1]["ok"] = True
                tools[-1]["result"] = _clip(line.strip(), 120)
    return {"events": events, "tools": tools, "compactions": compactions, "iters": iters,
            "max_iter": max_iter, "stop": stop}


def _max_parallel(nodes: list[dict]) -> int:
    marks = []
    for n in nodes:
        if n["kind"] != "task" and n["start"] is not None and n["end"] is not None:
            marks += [(n["start"], 1), (n["end"], -1)]
    cur = best = 0
    for _, d in sorted(marks, key=lambda x: (x[0], x[1])):
        cur += d
        best = max(best, cur)
    return best


def _tokens_by_model(nodes: list[dict]) -> dict:
    out = Counter()
    for n in nodes:
        out[n["model"] or "不明"] += n["tokens"]
    return dict(out)


def load_run(run_id: str) -> dict:
    if not ID_RE.match(run_id):
        raise ValueError("不正な Run ID")
    # 全ノードのログ正規表現パースは大きな Run では無視できないため、
    # ファイルが変わらない間は結果を再利用する(表示と「説明」で二重に
    # 走っていた。2026-10-27監査)
    return _cached(RUNS_DIR / f"{run_id}.json", "trace",
                   lambda: _load_run_uncached(run_id))


def _load_run_uncached(run_id: str) -> dict:
    d = json.loads((RUNS_DIR / f"{run_id}.json").read_text(encoding="utf-8"))
    t0 = d.get("created_at") or 0
    raw_nodes = (d.get("snapshot") or {}).get("nodes") or []
    # 古い記録はノードにモデル名が無いので、Run 全体のモデルで補う(画面では「Run の既定」と表示)
    run_model = d.get("model_tag") or d.get("model") or ""
    nodes = []
    for n in raw_nodes:
        start, end = n.get("started_at"), n.get("finished_at")
        rel = lambda x: round(x - t0, 2) if isinstance(x, (int, float)) and x > 0 else None
        s, e = rel(start), rel(end)
        dur = round(e - s, 2) if s is not None and e is not None else None
        log = _parse_log(n.get("log") or [])
        tok = n.get("tokens") or 0
        nodes.append({
            "id": n["id"], "parent": n.get("parent_id"), "kind": n.get("kind"), "title": _clip(n.get("title") or "", 80),
            "status": n.get("status"), "model": n.get("model_tag") or n.get("model_key") or run_model, "tokens": tok,
            "start": s, "end": e, "dur": dur, "tps": round(tok / dur, 1) if dur and tok else None,
            "ctx_fill": n.get("ctx_fill"), "prompt_chars": len(n.get("prompt") or ""),
            "output_chars": len(n.get("output") or ""), "think_chars": len(n.get("think") or ""), **log})
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:  # 親子の意味(矢印の説明)
        p = by_id.get(n["parent"])
        n["edge"] = EDGE_MEANING.get((p["kind"], n["kind"])) if p else None
        if p and not n["edge"]:
            n["edge"] = f"{p['kind']} が {n['kind']} に作業を渡す。"
    tools = [t for n in nodes for t in n["tools"]]
    run = {"id": d.get("id", run_id), "task": _clip(d.get("task") or "", 300), "mode": d.get("mode"),
           "status": d.get("status"), "model": d.get("model"), "model_tag": d.get("model_tag"),
           "tokens": d.get("tokens"),
           "duration": round(d["finished_at"] - t0, 1) if d.get("finished_at") and t0 else None}
    stats = {"nodes": len(nodes), "by_kind": dict(Counter(n["kind"] for n in nodes)),
             "max_parallel": _max_parallel(nodes), "tokens": sum(n["tokens"] for n in nodes),
             "tokens_by_model": _tokens_by_model(nodes),
             "tool_calls": len(tools), "tool_errors": sum(1 for t in tools if t["ok"] is False),
             "tool_names": dict(Counter(t["name"] for t in tools).most_common(8)),
             "compactions": sum(len(n["compactions"]) for n in nodes),
             "max_iter_hits": sum(1 for n in nodes if n["stop"] == "max_iter"),
             "errors": sum(1 for n in nodes if n["status"] == "error")}
    return {"run": run, "nodes": nodes, "stats": stats, "candidates": candidates(nodes, stats)}


def candidates(nodes: list[dict], stats: dict) -> list[dict]:
    """記録から機械的に見つかる改善の候補。削減量は出さず、根拠の数字だけを示す。"""
    out = []

    def add(key, level, title, evidence, action):
        out.append({"key": key, "level": level, "title": title, "evidence": evidence, "action": action})

    edit_err = sum(1 for n in nodes for t in n["tools"] if t["name"] == "edit_file" and t["ok"] is False)
    if edit_err >= 2:
        add("edit", "high", f"edit_file の失敗が {edit_err} 回", "置換元の文字列が見つからない失敗が繰り返されている。",
            "編集の前に read_file で現在の内容を読む。小さく一意な範囲を置換する。")
    if stats["max_iter_hits"]:
        add("maxiter", "high", f"反復の上限で停止したエージェントが {stats['max_iter_hits']} 体", "目標を終える前に max_iter に達した。",
            "目標を小さく分ける。計画(PLAN)で完了条件を明確にする。必要なら max_iter を上げる。")
    tight = [c for n in nodes for c in n["compactions"] if c["limit"] and c["used"] / c["limit"] > 0.6]
    if tight:
        worst = max(tight, key=lambda c: c["used"] / c["limit"])
        add("ctx", "mid", f"履歴圧縮が {len(tight)} 回(文脈の上限に近い)", f"最大で {worst['used']:,}/{worst['limit']:,} トークン使用。",
            "ツール出力を絞る(ファイル全体ではなく範囲を読む)。models.yaml の num_ctx を上げる余地を確認する。")
    cmd_fail = sum(1 for n in nodes for t in n["tools"] if t["name"] == "run_command" and t["ok"] is False)
    if cmd_fail >= 3:
        add("cmd", "mid", f"コマンドの失敗が {cmd_fail} 回", "run_command の exit≠0 が繰り返されている。",
            "失敗した出力を読んでから次の手を打つよう、プロンプトで求める。環境依存のコマンドを避ける。")
    heavy_think = [n for n in nodes if n["think_chars"] > 2 * max(1, n["output_chars"]) and n["think_chars"] > 4000]
    if heavy_think:
        add("think", "low", f"思考が出力の2倍を超えたエージェントが {len(heavy_think)} 体", "思考(推論)の字数が出力より大幅に多い。",
            "定型の作業では think を切るか、軽いモデルに任せる。")
    subs = [n for n in nodes if n["kind"] == "subagent"]
    if len(subs) >= 3 and stats["max_parallel"] <= 1:
        add("parallel", "mid", "子エージェントが並列に動いていない", f"子エージェント {len(subs)} 体に対して同時実行は最大 {stats['max_parallel']}。",
            "VRAM に全部載るモデル(placement: vram)を使うと並列にできる。hybrid のモデルは直列になる。")
    big_prompt = [n for n in nodes if n["prompt_chars"] > 20000]
    if big_prompt:
        add("prompt", "low", f"大きな入力を受け取ったエージェントが {len(big_prompt)} 体", f"最大 {max(n['prompt_chars'] for n in big_prompt):,} 字。",
            "子へ渡す文脈を必要な部分だけにする。")
    order = {"high": 0, "mid": 1, "low": 2}
    return sorted(out, key=lambda c: order[c["level"]])


def facts_for_analyst(trace: dict) -> dict:
    """分析エージェントに渡す事実(数値と種類だけ。本文は渡さない)。"""
    r, s = trace["run"], trace["stats"]
    slow = sorted((n for n in trace["nodes"] if n["dur"]), key=lambda n: -n["dur"])[:5]
    return {"種類": "ai-agent-lab の Run", "モード": r["mode"], "状態": r["status"], "所要秒": r["duration"],
            "統計": s, "時間のかかったエージェント": [{"種類": n["kind"], "モデル": n["model"], "秒": n["dur"],
                                                  "トークン": n["tokens"], "tok/s": n["tps"], "ツール": len(n["tools"])} for n in slow],
            "自動検出した改善候補": [{"題": c["title"], "根拠": c["evidence"]} for c in trace["candidates"]]}
