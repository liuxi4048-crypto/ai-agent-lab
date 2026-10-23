"""cc-session-lab の入口。モデルが読むのはこのスクリプトの短い出力だけにする。

  python -I lab.py complexity [--session ID|PATH]          今の依頼が複雑か(JSON)
  python -I lab.py build [--session ...] [--out DIR]        ダッシュボードを生成(要約 JSON)
                         [--extra-facts F.json] [--experiments F.json]
  python -I lab.py tokens [--session ...]                   トークン改善の候補(テキスト)
  python -I lab.py glossary [語]                            用語の説明(語なしで一覧)
  python -I lab.py sessions                                 最近のセッション一覧

セッション未指定時は CLAUDE_CODE_SESSION_ID → 作業ディレクトリのプロジェクトの最新 → 全体の最新 の順に探す。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze  # noqa: E402
import build as builder  # noqa: E402
import collect_env  # noqa: E402
import extract_trace  # noqa: E402

SKILL = HERE.parent
PROJECTS = collect_env.CLAUDE / "projects"


def find_session(arg=None):
    if arg:
        p = Path(arg)
        if p.suffix == ".jsonl" and p.exists():
            return p
        hits = sorted(PROJECTS.glob(f"*/{arg}*.jsonl"))
        if hits:
            return hits[0]
        sys.exit(f"セッションが見つかりません: {arg}")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        hits = list(PROJECTS.glob(f"*/{sid}.jsonl"))
        if hits:
            return hits[0]
    # プロジェクトのフォルダ名は cwd の英数字以外を - にしたもの(公式 S1)
    here = PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd()))
    for pool in (here.glob("*.jsonl"), PROJECTS.glob("*/*.jsonl")):
        files = sorted(pool, key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files[0]
    sys.exit(f"セッション記録がありません: {PROJECTS}")


def out_dir(arg, trace):
    if arg:
        return Path(arg)
    cfg = SKILL / "config.json"
    base = (json.loads(cfg.read_text(encoding="utf-8")).get("out_dir") if cfg.exists() else None) \
        or os.environ.get("CC_SESSION_LAB_OUT") or Path.home() / "cc-session-lab"
    # 同じセッションは同じフォルダへ上書きする
    return Path(base).expanduser() / f"{trace['session']['start_iso'][:10]}-{trace['session']['id'][:8]}"


def emit(obj):
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_complexity(a):
    src = find_session(a.session)
    res = analyze.complexity(extract_trace.parse(src), extract_trace.summarize, extract_trace.ts)
    emit({"session": src.stem, **{k: res[k] for k in ("verdict", "score", "reason")},
          "signals": {s["id"]: f"{s['value']}/{s['threshold']}{' ✓' if s['hit'] else ''}" for s in res.get("signals", [])}})


def cmd_build(a):
    src = find_session(a.session)
    trace = extract_trace.extract(src)
    env = collect_env.collect()
    extra = json.loads(Path(a.extra_facts).read_text(encoding="utf-8"))["facts"] if a.extra_facts else []
    exps = json.loads(Path(a.experiments).read_text(encoding="utf-8"))["experiments"] if a.experiments else []
    token = analyze.token_report(trace)
    res = builder.build(out_dir(a.out, trace), env, trace, analyze.findings(trace, env), token, extra, exps)
    cx = analyze.complexity(extract_trace.parse(src), extract_trace.summarize, extract_trace.ts)
    emit({**res, "session": src.stem, "complexity": f"{cx['verdict']}({cx['reason']})",
          "token_top": [f"{c['title']}(概算 {c['est_tokens']:,})" for c in token["candidates"][:3]]})


def cmd_tokens(a):
    src = find_session(a.session)
    rep = analyze.token_report(extract_trace.extract(src))
    m = rep["metrics"]
    sys.stdout.reconfigure(encoding="utf-8")
    if not m:
        print("API 呼び出しの記録がありません。")
        return
    print(f"セッション {src.stem[:8]}… 呼び出し {m['calls']} 回 / 固定費 {m['first_total']:,} / 最大 {m['peak_total']:,} / "
          f"再利用率 {m['cache_read_sum'] / max(1, m['input_sum']):.1%} / 出力 {m['output_sum']:,}(思考 {m['thinking_sum']:,})")
    for c in rep["candidates"]:
        print(f"- [{c['level']}] {c['title']}(概算 {c['est_tokens']:,})\n  根拠: {c['evidence']}\n  対策: {c['action']}")


def cmd_glossary(a):
    g = json.loads((SKILL / "data" / "glossary.json").read_text(encoding="utf-8"))["terms"]
    sys.stdout.reconfigure(encoding="utf-8")
    if not a.term:
        for t in g:
            print(f"{t['term']}: {t['short']}")
        return
    q = a.term.lower()
    names = lambda t: [x.lower() for x in (t["term"], *t["aliases"])]
    # 完全一致 → 前方一致 → 部分一致 → 説明文 の順に並べる
    rank = lambda t: 0 if q in names(t) else 1 if any(n.startswith(q) for n in names(t)) else 2
    hits = sorted((t for t in g if any(q in n for n in names(t))), key=rank) or \
           [t for t in g if q in (t["short"] + t["detail"]).lower()]
    for t in hits[:5]:
        print(f"■ {t['term']}({t['cat']})\n{t['short']}\n{t['detail']}\n関連: {', '.join(t['related'])}\n")
    if not hits:
        print("用語集に該当なし")


def cmd_sessions(a):
    files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    sys.stdout.reconfigure(encoding="utf-8")
    for p in files:
        print(f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}  {p.stem}  {p.stat().st_size // 1024} KB  {p.parent.name[-40:]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("complexity", "build", "tokens"):
        p = sub.add_parser(name)
        p.add_argument("--session")
        if name == "build":
            p.add_argument("--out")
            p.add_argument("--extra-facts")
            p.add_argument("--experiments")
    sub.add_parser("glossary").add_argument("term", nargs="?")
    sub.add_parser("sessions")
    a = ap.parse_args()
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
