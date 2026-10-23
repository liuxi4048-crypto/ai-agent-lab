"""Claude Code のセッション記録を、取り込んだ cc-session-lab(insight/cclab)で可視化する。

ダッシュボードは runs/insight-cache/cc/<セッションID先頭8桁>/ に書き出し、server.py が
/insight-cc/ として配信する。runs/ は .gitignore 済みなので、記録由来の内容はリポジトリに入らない。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "cclab" / "scripts"))
import analyze  # noqa: E402
import build as cc_build  # noqa: E402
import collect_env  # noqa: E402
import extract_trace  # noqa: E402

CACHE = HERE.parent / "runs" / "insight-cache" / "cc"
PROJECTS = collect_env.CLAUDE / "projects"
SID_RE = re.compile(r"^[0-9a-f-]{8,36}$")


def available() -> bool:
    return PROJECTS.is_dir()


def list_sessions(limit: int = 30) -> list[dict]:
    if not available():
        return []
    files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"id": p.stem, "project": p.parent.name[-48:], "size_kb": p.stat().st_size // 1024,
             "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")} for p in files]


def _find(sid: str) -> Path:
    if not SID_RE.match(sid):
        raise ValueError("不正なセッション ID")
    hits = list(PROJECTS.glob(f"*/{sid}*.jsonl"))
    if not hits:
        raise FileNotFoundError(sid)
    return hits[0]


def build(sid: str) -> dict:
    """ダッシュボードを作り直し、表示用の URL と要約を返す。同じセッションは同じフォルダへ上書きする。"""
    src = _find(sid)
    trace = extract_trace.extract(src)
    env = collect_env.collect()
    token = analyze.token_report(trace)
    out = CACHE / src.stem[:8]
    cc_build.build(out, env, trace, analyze.findings(trace, env), token)
    return {"url": f"/insight-cc/{src.stem[:8]}/index.html", "session": src.stem,
            "token_top": token["candidates"][:3], "metrics": token["metrics"]}


def facts_for_analyst(sid: str) -> dict:
    """分析エージェントに渡す事実(数値と所見だけ。会話の本文は渡さない)。"""
    src = _find(sid)
    trace = extract_trace.extract(src)
    token = analyze.token_report(trace)
    cx = analyze.complexity(extract_trace.parse(src), extract_trace.summarize, extract_trace.ts)
    return {"種類": "Claude Code のセッション", "版": trace["session"]["version"],
            "API呼び出し": len(trace["api_calls"]), "ツール呼び出し": len(trace["tools"]),
            "サブエージェント": len(trace["subagents"]), "トークン指標": token["metrics"],
            "自動所見": [f["claim"] for f in analyze.findings(trace, {})],
            "自動検出した改善候補": [{"題": c["title"], "根拠": c["evidence"], "概算": c["est_tokens"]} for c in token["candidates"]],
            "直近の依頼の複雑度": {"判定": cx["verdict"], "理由": cx["reason"]}}
