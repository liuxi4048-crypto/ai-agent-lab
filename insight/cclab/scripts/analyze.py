"""トレースから自動所見(分類 B)・トークン改善候補・複雑度判定を作る。

どれも trace(extract_trace.extract の戻り値)だけを入力にし、モデルの推測は混ぜない。
削減量の見積もりは概算(分類 D)として扱う。
"""
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

CHARS_PER_TOKEN = 3.0  # 日英混在・JSON を含む文の概算。正確なトークン数は usage にしか無い
HOME = str(Path.home())


def _home(p):
    return p.replace(HOME, "~") if isinstance(p, str) else p


def _status(t):
    if (t.get("permission") or {}).get("decision") == "reject":
        return "deny"
    if t.get("end") is None:
        return "run"
    return "err" if t.get("is_error") else "ok"


def _shapes(events):
    return [e for e in events if e["kind"] == "request_shape"]


def _first_attachments(trace):
    calls = trace["api_calls"]
    t1 = calls[0]["t"] if calls else float("inf")
    return [e for e in trace["events"] if e["kind"] == "attachment" and e["t"] <= t1 and e["detail"].get("rendered_chars")]


def _spikes(calls):
    # 2回目以降で書込が大きい回はキャッシュの作り直しを疑う(app.js と同じ基準)
    return [c for i, c in enumerate(calls) if i > 0 and c["cache_write"] > max(20000, 0.3 * c["context_total"])]


def findings(trace, env):
    """このセッションの実測から機械的に言える事実だけを返す。"""
    sid = trace["session"]["id"][:8]
    ev = f"このセッション {sid}…(trace.json)"
    calls, tools, out = trace["api_calls"], trace["tools"], []

    def add(i, topic, claim):
        out.append({"id": f"BX-{i}", "cls": "B", "topic": topic, "claim": claim, "evidence": ev})

    s = trace["session"]
    add("ENV", "env", f"Claude Code {s['version']}(entrypoint={s['entrypoint']})。PATH 上の claude は {env.get('claude_code', {}).get('version') or '不明'}。")
    if calls:
        c = calls[0]
        add("CTX-1", "context", f"最初の API 呼び出しの入力は {c['context_total']:,} トークン(キャッシュ読取 {c['cache_read']:,} / 書込 {c['cache_write']:,} / 非キャッシュ {c['input']:,})。")
        rate = sum(x["cache_read"] for x in calls) / max(1, sum(x["context_total"] for x in calls))
        add("CACHE", "cache", f"API 呼び出し {len(calls)} 回の入力合計のうち {rate:.1%} をキャッシュから再利用。書込が急増した回(作り直しの疑い)は {len(_spikes(calls))} 回。")
        peak = max(calls, key=lambda x: x["context_total"])
        add("CTX-5", "context", f"1回あたりの入力の最大は {peak['context_total']:,} トークン。出力の合計は {sum(x['output'] for x in calls):,} トークン。")
    sh = _shapes(trace["events"])
    if sh:
        d = sh[0]["detail"]
        add("CTX-2", "context", f"リクエストの system は {len(d['system_blocks_chars'])} ブロック({' / '.join(f'{n:,}' for n in d['system_blocks_chars'])} 字)、tools は {d['tools']} 個・JSON 約 {d['tools_chars']:,} 字。")
        counts = [x["detail"]["tools"] for x in sh]
        if len(set(counts)) > 1:
            add("CTX-4", "context", f"会話の途中で tools の数が {' → '.join(map(str, dict.fromkeys(counts)))} と変化した(ToolSearch による遅延ツールの読込など)。")
    atts = sorted(_first_attachments(trace), key=lambda e: -e["detail"]["rendered_chars"])[:4]
    if atts:
        add("CTX-3", "context", "最初の呼び出しまでの大きな添付: " + "、".join(f"{e['label']} {e['detail']['rendered_chars']:,} 字" for e in atts) + "。")
    ins = [f["path"] for e in trace["events"] if e["label"] == "instructions" for f in e["detail"].get("files", [])]
    add("INS", "memory", "読み込まれた指示ファイル: " + ("、".join(dict.fromkeys(_home(p) for p in ins)) if ins else "記録なし") + "。")
    if tools:
        st = Counter(_status(t) for t in tools)
        batches = Counter(t["msg"] for t in tools if t["batch_size"] > 1)
        add("TOOL-1", "tools", f"メインのツール呼び出しは {len(tools)} 回(成功 {st['ok']} / エラー {st['err']} / 権限拒否 {st['deny']} / 未完了 {st['run']})。並列バッチ {len(batches)} 回、最大 {max([t['batch_size'] for t in tools])} 件同時。")
        top = Counter(t["name"] for t in tools).most_common(5)
        add("TOOL-2", "tools", "よく使ったツール: " + "、".join(f"{n} {k} 回" for n, k in top) + "。")
        denied = [t for t in tools if _status(t) == "deny"]
        if denied:
            nohook = sum(1 for t in denied if not any(h.get("event") == "PreToolUse" for h in t.get("hooks", [])))
            add("TOOL-3", "hooks", f"権限で拒否された呼び出し {len(denied)} 件のうち {nohook} 件では PreToolUse フックの記録が無い。")
    subs = trace["subagents"]
    if subs:
        add("SUB", "subagents", "サブエージェント: " + "、".join(
            f"{x['meta'].get('agentType')}({'/'.join(x['models']) or '—'}・ツール {len(x['tools'])} 回)" for x in subs) + "。親に戻るのは最終結果のみ。")
    cmp = [e for e in trace["events"] if e["kind"] == "system" and "compact" in e["label"]]
    add("CMP", "compaction", f"このセッションの圧縮(compact_boundary)は {len(cmp)} 回。")
    return out


def token_report(trace):
    """固定費・キャッシュ・大きな出力・重複読込・サブエージェントから改善候補を作る。"""
    calls, tools, cands = trace["api_calls"], trace["tools"], []
    if not calls:
        return {"candidates": [], "metrics": {}}
    first = calls[0]
    sh = _shapes(trace["events"])
    tools_chars = sh[0]["detail"]["tools_chars"] if sh else 0
    sys_chars = sum(sh[0]["detail"]["system_blocks_chars"]) if sh else 0
    atts = _first_attachments(trace)
    att_chars = sum(e["detail"]["rendered_chars"] for e in atts)
    # 按分の分母は画面(app.js の構成表)と同じく、最初の呼び出しまでのユーザー入力も含める
    user_chars = sum(e["detail"]["chars"] for e in trace["events"] if e["kind"] == "user_prompt" and e["t"] <= first["t"])
    total_chars = max(1, tools_chars + sys_chars + att_chars + user_chars)

    def tok(chars):  # 字数比で最初の呼び出しの実測トークンを按分する
        return round(first["context_total"] * chars / total_chars)

    def cand(key, title, evidence, action, est):
        cands.append({"key": key, "title": title, "evidence": evidence, "action": action, "est_tokens": est,
                      "level": "high" if est >= 50000 else "mid" if est >= 10000 else "low"})

    if tools_chars / total_chars > 0.35:
        cand("tools", "ツール定義が固定費の大半を占める", f"最初のリクエストの約 {tools_chars / total_chars:.0%}({tools_chars:,} 字)がツール定義。",
             "使わない MCP サーバーやツールを無効化する。deny に裸のツール名を書くと定義ごと外れる。", tok(tools_chars))
    for e in sorted(atts, key=lambda e: -e["detail"]["rendered_chars"]):
        if e["label"] in ("skill_listing", "agent_listing_delta", "mcp_instructions_delta") and e["detail"]["rendered_chars"] > 8000:
            what = {"skill_listing": "スキル一覧", "agent_listing_delta": "エージェント一覧", "mcp_instructions_delta": "MCP の説明"}[e["label"]]
            cand(e["label"], f"{what}が毎回送られている", f"{e['label']} が {e['detail']['rendered_chars']:,} 字。",
                 "使わないものを無効化する、説明文を短くする、disable-model-invocation やパック化で一覧から外す。", tok(e["detail"]["rendered_chars"]))
    sp = _spikes(calls)
    if sp:
        cand("cache", f"キャッシュの作り直しが {len(sp)} 回", "書込が急増した回: " + "、".join(f"#{calls.index(c) + 1}({c['cache_write']:,})" for c in sp[:5]),
             "長い待ち(TTL 超え)を挟まない。途中でモデル・effort・MCP 接続・ツール構成を変えない。", sum(c["cache_write"] for c in sp))
    # 大きなツール出力は、以降の毎回の呼び出しで再送される(キャッシュ読取でも量は数える)
    later = lambda t: sum(1 for c in calls if c["t"] > t["start"])
    big = sorted((t for t in tools if t.get("output_chars", 0) > 8000), key=lambda t: -t["output_chars"])[:5]
    if big:
        cand("output", f"大きなツール出力が {len(big)} 件", "、".join(f"{t['name']} {t['output_chars']:,} 字" for t in big),
             "head・grep・Read の offset/limit で必要な部分だけ取る。大量の調査はサブエージェントに任せて要約だけ受け取る。",
             round(sum(t["output_chars"] / CHARS_PER_TOKEN for t in big)))
    reads = Counter(f for t in tools if t["name"] == "Read" for f in t.get("files", []))
    dup = [(f, n) for f, n in reads.items() if n >= 2]
    if dup:
        est = sum(t.get("output_chars", 0) for t in tools if t["name"] == "Read" and t.get("files") and reads[t["files"][0]] >= 2) / CHARS_PER_TOKEN
        cand("reread", f"同じファイルを複数回 Read({len(dup)} ファイル)", "、".join(f"{Path(f).name} ×{n}" for f, n in dup[:5]),
             "一度読んだ内容は会話に残っている。読み直しは変更後だけにする。", round(est / 2))
    subs = trace["subagents"]
    boot = [s["api_calls"][0]["cache_write"] for s in subs if s["api_calls"]]
    if boot:
        cand("subagent", f"サブエージェントの起動コスト({len(boot)} 体)", f"各サブエージェントの最初の呼び出しで新規にキャッシュへ書いた量の合計 {sum(boot):,}。",
             "小さな作業は自分で行う。同じ種類の作業はまとめて1体に渡す(同種の並列起動はキャッシュを共有できる)。", sum(boot))
    out_sum = sum(c["output"] for c in calls)
    think = sum(c.get("thinking_tokens") or 0 for c in calls)
    if out_sum > 20000 and think / max(1, out_sum) > 0.6:
        cand("thinking", "出力の大半が思考", f"出力 {out_sum:,} トークンのうち思考 {think:,}({think / out_sum:.0%})。",
             "定型的な作業では effort を下げる。", round(think * 0.3))
    peak = max(c["context_total"] for c in calls)
    if peak > 200000:
        cand("growth", "会話が長くなっている", f"1回あたりの入力が最大 {peak:,} トークン。", "区切りの良いところで /compact するか、新しいセッションに分ける。", peak - first["context_total"])
    cands.sort(key=lambda c: -c["est_tokens"])
    return {"candidates": cands, "metrics": {
        "calls": len(calls), "first_total": first["context_total"], "peak_total": peak,
        "input_sum": sum(c["context_total"] for c in calls), "cache_read_sum": sum(c["cache_read"] for c in calls),
        "cache_write_sum": sum(c["cache_write"] for c in calls), "output_sum": out_sum, "thinking_sum": think,
        "first_request_chars": {"system": sys_chars, "tools": tools_chars, "attachments": att_chars}}}


# 複雑度の信号。閾値は「1回の依頼として明らかに大きい」水準に置き、迷う作業は simple 側へ倒す
SIGNALS = [
    ("tools", "ツール呼び出しが多い", lambda m: m["tool_calls"], 20),
    ("delegation", "サブエージェントか並列実行を使った", lambda m: m["subagents"] + (1 if m["parallel_batches"] >= 3 else 0), 1),
    ("edits", "複数ファイルを変更した", lambda m: m["edited_files"], 3),
    ("retries", "失敗と再試行があった", lambda m: m["errors"], 2),
    ("context", "文脈が大きい/圧縮が起きた", lambda m: 1 if m["peak_context"] >= 150000 or m["compactions"] else 0, 1),
    ("duration", "長時間・多数回の問い合わせ", lambda m: 1 if m["minutes"] >= 10 and m["api_calls"] >= 15 else 0, 1),
]


def _is_human_prompt(r):
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"):
        return False
    origin = (r.get("origin") or {}).get("kind")
    if origin and origin != "human":
        return False
    c = r["message"]["content"]
    return isinstance(c, str) or not any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c)


def complexity(recs, summarize, ts):
    """直近のユーザー入力以降(=今の依頼)を対象に6信号を数え、3つ以上で complex。"""
    idx = max((i for i, r in enumerate(recs) if _is_human_prompt(r)), default=0)
    win = recs[idx:]
    stamped = [r for r in win if "timestamp" in r]
    if not stamped:
        return {"verdict": "simple", "score": 0, "signals": [], "reason": "記録がありません"}
    t0 = ts(stamped[0]["timestamp"])
    tools, calls, events = summarize(win, t0)
    edited = {f for t in tools if t["name"] in ("Write", "Edit", "NotebookEdit") for f in t["files"]}
    m = {"tool_calls": len(tools), "subagents": sum(1 for t in tools if t["name"] == "Agent"),
         "parallel_batches": len({t["msg"] for t in tools if t["batch_size"] > 1}),
         "edited_files": len(edited), "errors": sum(1 for t in tools if t.get("is_error")),
         "peak_context": max((c["context_total"] for c in calls), default=0),
         "compactions": sum(1 for e in events if e["kind"] == "system" and "compact" in e["label"]),
         "api_calls": len(calls), "minutes": round((ts(stamped[-1]["timestamp"]) - t0) / 60000, 1)}
    sig = [{"id": k, "label": lab, "value": f(m), "threshold": th, "hit": f(m) >= th} for k, lab, f, th in SIGNALS]
    score = sum(s["hit"] for s in sig)
    verdict = "complex" if score >= 3 else "borderline" if score == 2 else "simple"
    return {"verdict": verdict, "score": score, "signals": sig, "metrics": m,
            "window_from": datetime.fromtimestamp(t0 / 1000).isoformat(timespec="seconds"),
            "reason": "、".join(s["label"] for s in sig if s["hit"]) or "目立つ信号なし"}
