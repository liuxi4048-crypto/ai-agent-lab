"""セッションのトランスクリプト(JSONL)から実測トレースを抽出する(lab.py から使う)。

単体実行: python -I extract_trace.py <session.jsonl> <out.json>
- ツール入力は160字・出力は240字で切り、トークン様の文字列は伏せる
- システムプロンプト・CLAUDE.md・添付の本文は保存しない(種別・文字数・パスだけ)
"""
import json, re, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

TOKEN_LIKE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|[A-Za-z0-9_\-]{40,})")
SECRET_KV = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)(\"?\s*[=:]\s*\"?|\s+)([^\s\",}]+)")
SECRET_KEY = re.compile(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|authorization|credential|cookie)")
AUTH_VALUE = re.compile(r"(?i)\b(bearer|basic)\s+\S+")
EXP_MARK = re.compile(r"EXP:([A-Z]):")
FILE_KEYS = ("file_path", "path", "notebook_path")


def scrub(obj):
    """辞書は JSON 化の前に、秘密らしいキーの値を丸ごと伏せる(入れ子も)。"""
    if isinstance(obj, dict):
        return {k: "<REDACTED>" if SECRET_KEY.search(str(k)) else scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def clip(s, n):
    s = s if isinstance(s, str) else json.dumps(scrub(s), ensure_ascii=False)
    s = AUTH_VALUE.sub(r"\1 <REDACTED>", s)
    s = TOKEN_LIKE.sub("<REDACTED>", SECRET_KV.sub(r"\1\2<REDACTED>", s))
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n}字)"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    return str(content)


def parse(path, until=None):
    recs = [json.loads(l) for l in open(path, encoding="utf-8")]
    if until:
        recs = [r for r in recs if "timestamp" not in r or ts(r["timestamp"]) <= until]
    return recs


def summarize(recs, t0, agent="main"):
    tools, api_calls, events, hooks_by_tool = {}, [], [], defaultdict(list)
    seen_msg = {}
    exp = None
    for r in recs:
        t = r.get("type")
        if "timestamp" not in r:
            continue
        rt = ts(r["timestamp"]) - t0
        if t == "attachment":
            a = r["attachment"]
            at = a["type"]
            if at.startswith("hook_"):
                h = {"event": a.get("hookEvent"), "name": a.get("hookName"), "ms": a.get("durationMs"),
                     "exit": a.get("exitCode"), "t": rt, "command": clip(a.get("command", ""), 120)}
                if a.get("toolUseID"):
                    hooks_by_tool[a["toolUseID"]].append(h)
                else:
                    events.append({"t": rt, "kind": "hook", "label": h["name"], "detail": h, "agent": agent})
                continue
            info = {"type": at, "rendered_chars": sum(len(x.get("content", "")) for x in r.get("rendered") or [])}
            if at == "instructions":
                info["files"] = [{"path": f.get("path"), "scope": f.get("type"), "chars": len(f.get("content", ""))}
                                 for f in a.get("files", [])]
            elif at == "skill_listing":
                info["skillCount"] = a.get("skillCount")
            elif at == "deferred_tools_delta":
                info["added"] = len(a.get("addedNames", []))
                info["needsAuth"] = len(a.get("needsAuthMcpServers", []))
            elif at == "agent_listing_delta":
                info["added"] = len(a.get("addedTypes", []))
            elif at == "prompt_snapshot":
                info["systemPromptSections"] = len(a.get("systemPrompt", []))
                info["systemPromptChars"] = sum(len(s) for s in a.get("systemPrompt", []))
            elif at == "environment":
                info["snapshot_keys"] = sorted((a.get("snapshot") or {}).keys())
            events.append({"t": rt, "kind": "attachment", "label": at, "detail": info, "agent": agent})
        elif t == "api-request":
            p = r.get("params", {})
            events.append({"t": rt, "kind": "api_request", "label": "API リクエスト送信", "agent": agent,
                           "detail": {"model": p.get("model"), "max_tokens": p.get("max_tokens"),
                                      "thinking": p.get("thinking"), "effort": (p.get("output_config") or {}).get("effort"),
                                      "betas": len(p.get("betas", [])), "context_management": p.get("context_management"),
                                      "cacheControl": r.get("cacheControl"), "querySource": r.get("querySource")}})
        elif t == "api-request-shape":
            sh = r.get("shape", {})
            events.append({"t": rt, "kind": "request_shape", "label": "リクエスト形状", "agent": agent,
                           "detail": {"system_blocks_chars": [len(json.dumps(b, ensure_ascii=False)) for b in sh.get("system", [])],
                                      "tools": len(sh.get("tools", [])),
                                      "tool_names": [x.get("name") for x in sh.get("tools", [])],
                                      "tools_chars": len(json.dumps(sh.get("tools", []), ensure_ascii=False))}})
        elif t == "system":
            events.append({"t": rt, "kind": "system", "label": r.get("subtype", "system"), "agent": agent,
                           "detail": {"compactMetadata": r.get("compactMetadata")}})
        elif t == "assistant":
            m = r["message"]
            mid = m.get("id")
            if mid not in seen_msg:
                call = {"id": mid, "t": rt, "model": m.get("model"), "thinkingMs": r.get("thinkingDurationMs"),
                        "effort": r.get("effort"), "stop_reason": None, "blocks": [], "agent": agent}
                seen_msg[mid] = call
                api_calls.append(call)
            call = seen_msg[mid]
            # 同じ message.id の後続ブロックで usage が更新されるので毎回上書きする
            u = m.get("usage") or {}
            call.update(input=u.get("input_tokens", 0), cache_read=u.get("cache_read_input_tokens", 0),
                        cache_write=u.get("cache_creation_input_tokens", 0), output=u.get("output_tokens", 0),
                        thinking_tokens=(u.get("output_tokens_details") or {}).get("thinking_tokens"))
            call["context_total"] = call["input"] + call["cache_read"] + call["cache_write"]
            call["thinkingMs"] = call["thinkingMs"] or r.get("thinkingDurationMs")
            if m.get("stop_reason"):
                call["stop_reason"] = m["stop_reason"]
            for b in m.get("content", []):
                call["blocks"].append(b.get("type"))
                if b.get("type") == "tool_use":
                    inp = b.get("input", {})
                    mk = EXP_MARK.search(json.dumps(inp, ensure_ascii=False))
                    if mk:
                        exp = mk.group(1)
                    files = [inp[k] for k in FILE_KEYS if isinstance(inp.get(k), str)]
                    tools[b["id"]] = {"id": b["id"], "name": b["name"], "start": rt, "msg": mid, "agent": agent,
                                      "input": clip(inp, 160), "files": files, "exp": exp,
                                      "description": clip(inp["description"], 160) if inp.get("description") else None}
                elif b.get("type") == "text":
                    events.append({"t": rt, "kind": "assistant_text", "label": "テキスト出力", "agent": agent,
                                   "detail": {"chars": len(b.get("text", ""))}})
        elif t == "user":
            c = r["message"]["content"]
            if isinstance(c, str) or not any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                if not r.get("isMeta") and not r.get("isCompactSummary"):
                    events.append({"t": rt, "kind": "user_prompt", "label": "ユーザー入力", "agent": agent,
                                   "detail": {"chars": len(text_of(c)), "origin": (r.get("origin") or {}).get("kind"),
                                              "permissionMode": r.get("permissionMode")}})
                continue
            for x in c:
                if isinstance(x, dict) and x.get("type") == "tool_result" and x["tool_use_id"] in tools:
                    tc = tools[x["tool_use_id"]]
                    tc["end"] = rt
                    tc["ms"] = round(rt - tc["start"])
                    tc["is_error"] = bool(x.get("is_error"))
                    out = text_of(x.get("content"))
                    tc["output"] = clip(out, 240)
                    tc["output_chars"] = len(out)
                    tc["permission"] = r.get("permissionDecision")
    # 使用量が全て0の応答(中断や合成メッセージ)は API 呼び出しとして数えない
    api_calls = [c for c in api_calls if c["context_total"] or c["output"]]
    for tid, hs in hooks_by_tool.items():
        if tid in tools:
            tools[tid]["hooks"] = hs
    # 同じ API 応答(message.id)内の複数 tool_use は並列バッチ
    by_msg = defaultdict(list)
    for tc in tools.values():
        by_msg[tc["msg"]].append(tc["id"])
    for tc in tools.values():
        tc["batch_size"] = len(by_msg[tc["msg"]])
    return list(tools.values()), api_calls, events


def extract(src, until=None):
    src = Path(src)
    recs = parse(src, until)
    t0 = min(ts(r["timestamp"]) for r in recs if "timestamp" in r)
    tools, api_calls, events = summarize(recs, t0)
    subdir = src.with_suffix("") / "subagents"
    subagents = []
    for meta in sorted(subdir.glob("*.meta.json")):
        aid = meta.name.split(".")[0]
        m = json.loads(meta.read_text(encoding="utf-8"))
        jl = subdir / f"{aid}.jsonl"
        srecs = parse(jl, until) if jl.exists() else []
        st, sa, se = summarize(srecs, t0, agent=aid)
        first_user = next((r for r in srecs if r.get("type") == "user"), None)
        subagents.append({"id": aid, "meta": m, "tools": st, "api_calls": sa, "events": se,
                          "prompt_chars": len(text_of(first_user["message"]["content"])) if first_user else 0,
                          "start": min((x["t"] for x in sa), default=None),
                          # 最終応答(end_turn)まで含める。最終応答が無ければ実行中として None
                          "end": max([x.get("end", x["start"]) for x in st] + [x["t"] for x in sa], default=None)
                          if any(x["stop_reason"] == "end_turn" for x in sa) else None,
                          "tokens_out": sum(x["output"] for x in sa),
                          "models": sorted({x["model"] for x in sa if x["model"]}),
                          "attachment_types": sorted({e["label"] for e in se if e["kind"] == "attachment"}),
                          "isSidechain": any(r.get("isSidechain") for r in srecs)})
    checks = {"nested_memory_attachment": any(e["label"] in ("nested_memory", "nested_instructions") for e in events)}
    out = {"session": {"id": src.stem, "version": next((r["version"] for r in recs if r.get("version")), None),
                       "entrypoint": next((r["entrypoint"] for r in recs if r.get("entrypoint")), None),
                       "start_iso": datetime.fromtimestamp(t0 / 1000).isoformat(timespec="seconds"),
                       "records": len(recs), "source": "~/.claude/projects/<encoded-cwd>/" + src.name},
           "evidence_class": "B", "tools": tools, "api_calls": api_calls, "events": sorted(events, key=lambda e: e["t"]),
           "subagents": subagents, "checks": checks}
    return out


if __name__ == "__main__":
    data = extract(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {sys.argv[2]}: tools={len(data['tools'])} api_calls={len(data['api_calls'])}")
