"""ローカルLLM性能ベンチ: 固定プロンプト集(bench_suite.yaml)を選んだモデルに投げ、
速度(tok/s・TTFT・ロード時間)と成果物の質(決定論チェック+LLM採点)を2軸で記録する。

設計:
- 1ベンチ=1Run(mode="bench")。Run/RunManager/EventBus/SSE/永続化は既存をそのまま使う
  (並列スロット・hybrid直列化・チェックポイント・UIの履歴表示が無償で付いてくる)。
- 3フェーズ直列: ①全課題を生成 → ②決定論チェック(LLM不使用) → ③judgeで全成果物を採点。
  OLLAMA_MAX_LOADED_MODELS=1 の環境で「対象モデル⇄judge」を課題ごとに交互ロードすると
  ロード時間(17〜65秒)が計測を汚すため、モデルのロードは計2回に抑える。
- tier ゲート(router.usable)は通さない。probation/archive のモデルを再ベンチして
  tier を確定させるのがこの機能の用途の1つ(models.yaml の pro 参照)。
- 速度は llm.chat が返す _timing メタ(初トークン実測 + Ollama 申告の eval_duration 等)。
- judge は絶対採点3基準(1〜5)。pairwise は位置バイアスが知られているため使わない。
  対象モデルと異ファミリーの judge を router.critique_pair で選ぶ(自己選好バイアス対策)。
- 結果は Run.bench(dict)に逐次書き、summary() 経由で runs/<id>.json と GET /runs に載る。
  UIはこれを読んで速度/品質パネルとモデル比較表を描く。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import statistics
import time

import yaml

import llm
import router
from artifacts import extract_files
from tools import Toolbox, js_syntax_check

SUITE_PATH = os.path.join(os.path.dirname(__file__), "bench_suite.yaml")
ARTIFACT_JUDGE_CAP = 7000      # judge に渡す成果物本文の上限(文字)。超過は先頭+末尾
JUDGE_CRITERIA = ("requirements", "correctness", "quality")
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {"type": "integer", "minimum": 1, "maximum": 5},
        "correctness": {"type": "integer", "minimum": 1, "maximum": 5},
        "quality": {"type": "integer", "minimum": 1, "maximum": 5},
        "comment": {"type": "string"},
    },
    "required": ["requirements", "correctness", "quality", "comment"],
}
_THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_FENCE_RE = re.compile(r"```(\w*)[^\n]*\n(.*?)```", re.DOTALL)
_JUDGE_FALLBACK_KEYS = ("reasoner", "worker", "coder")
# 無人実行の子プロセスへ渡さない環境変数(名前に秘密情報らしい語を含むもの)
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
MAX_REPS_PER_RUN = 30      # 課題数×反復の上限(1ベンチが何時間も走らないように)


def _safe_env() -> dict:
    """生成コードを動かす子プロセス用の環境変数。秘密情報らしいキーを剥がす。"""
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


def _bus_delta(bus, node_id: str):
    """llm.chat の on_delta → EventBus(本文/thinking)への振り分け。"""
    def on_delta(kind, piece):
        (bus.token_progress if kind == "content" else bus.think_progress)(node_id, piece)
    return on_delta


# ============================================================ suite ----
def load_suite(path: str = SUITE_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        suite = yaml.safe_load(f)
    tasks = suite.get("tasks") or []
    if not tasks:
        raise ValueError("bench_suite.yaml に tasks がありません")
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("bench_suite.yaml の task id が重複しています")
    return suite


def suite_catalog(suite: dict) -> dict:
    """UI用: 課題一覧(プロンプト本文も含める。ユーザーが何を投げるか確認できるように)。"""
    return {
        "version": suite.get("version"),
        "tasks": [{
            "id": t["id"], "title": t["title"], "category": t.get("category", ""),
            "kind": t.get("kind", "text"), "prompt": t["prompt"],
            "checks": [c["type"] for c in (t.get("checks") or [])],
        } for t in suite["tasks"]],
    }


def select_tasks(suite: dict, task_ids: list[str] | None) -> list[dict]:
    if not task_ids:
        return list(suite["tasks"])
    task_ids = list(dict.fromkeys(task_ids))   # 重複は1回に畳む(成果物ディレクトリの衝突防止)
    by_id = {t["id"]: t for t in suite["tasks"]}
    unknown = [i for i in task_ids if i not in by_id]
    if unknown:
        raise ValueError(f"未知の課題ID: {', '.join(unknown)}")
    return [by_id[i] for i in task_ids]


def pick_judge(cfg, model_key: str, judge: str, installed: set[str]) -> str | None:
    """judge モデルを決める。"none" なら LLM採点を省略(決定論チェックのみ)。

    auto は critique_pairs(異ファミリー)を優先し、未導入なら導入済みの候補から
    対象モデルと別ファミリーのものを拾う。同ファミリーしか無ければ None。
    """
    if judge == "none":
        return None

    def ok(key: str) -> bool:
        tag = llm.resolve(cfg, key)["tag"]
        return tag in installed or f"{tag}:latest" in installed

    if judge and judge != "auto":
        if not ok(judge):
            raise ValueError(f"judge モデル {judge} は未導入です")
        return judge
    cand = router.critique_pair(cfg, model_key)
    fam = llm.resolve(cfg, model_key)["family"]
    for key in (cand, *_JUDGE_FALLBACK_KEYS):
        if key and key != model_key and ok(key) and llm.resolve(cfg, key)["family"] != fam:
            return key
    return None


# ============================================================ 抽出 ----
def _strip_think(text: str) -> str:
    return _THINK_TAG_RE.sub("", text or "").strip()


def _fenced_blocks(text: str, langs: tuple[str, ...]) -> list[str]:
    return [m.group(2) for m in _FENCE_RE.finditer(text)
            if (m.group(1) or "").lower() in langs]


def _balanced_json(text: str) -> str | None:
    """最初の { または [ から対応する閉じ括弧までを切り出す(フェンス無し出力の保険)。"""
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_artifact(task: dict, content: str) -> tuple[str | None, str | None]:
    """応答本文から課題の成果物を取り出す。(ファイル名, 中身) / 取れなければ (None, None)。

    優先順: 「ファイル: 名前」明示ブロック → 言語一致のフェンス(最大のもの) → 生テキスト判定。
    単発生成なので、モデルがフェンス形式を守らなくても拾えるように段階的に緩める。
    """
    kind = task.get("kind", "text")
    text = _strip_think(content)
    if kind == "text":
        return "output.md", text
    if kind == "html":
        named = [(n, c) for n, c in extract_files(text) if n.lower().endswith((".html", ".htm"))]
        if named:
            return "index.html", named[-1][1]
        blocks = _fenced_blocks(text, ("html",))
        if blocks:
            return "index.html", max(blocks, key=len)
        if re.search(r"<!doctype html|<html", text, re.I):
            return "index.html", text
        return None, None
    if kind == "script":
        fname = task.get("file", "main.py")
        ext = os.path.splitext(fname)[1].lower()
        named = [(n, c) for n, c in extract_files(text) if n.lower().endswith(ext)]
        if named:
            return fname, named[-1][1]
        langs = ("python", "py") if ext == ".py" else ("javascript", "js") if ext == ".js" else ()
        blocks = _fenced_blocks(text, langs) or _fenced_blocks(text, ("",))
        if blocks:
            return fname, max(blocks, key=len)
        return None, None
    if kind == "json":
        blocks = _fenced_blocks(text, ("json",)) or _fenced_blocks(text, ("",))
        cand = max(blocks, key=len) if blocks else _balanced_json(text)
        return ("output.json", cand.strip()) if cand else (None, None)
    return "output.md", text


# ============================================================ 決定論チェック ----
def _re_flags(spec: str | None) -> int:
    flags = 0
    for ch in (spec or ""):
        flags |= {"i": re.I, "m": re.M, "s": re.S}.get(ch, 0)
    return flags


async def run_checks(task: dict, tb: Toolbox, artifact_name: str | None,
                     artifact_text: str | None, content: str) -> list[dict]:
    """課題の checks を順に評価する。各結果は {type, ok, detail}。

    成果物が取れなかった場合は全チェックを失敗にする(「何も出していない」を通さない)。
    """
    results = []
    body = _strip_think(content)
    for spec in task.get("checks") or []:
        ctype = spec["type"]
        ok, detail = False, ""
        try:
            if artifact_name is None and ctype != "regex" and ctype != "length":
                detail = "成果物を抽出できませんでした"
            elif ctype == "regex":
                ok = re.search(spec["pattern"], body, _re_flags(spec.get("flags"))) is not None
                detail = "一致" if ok else f"不一致: /{spec['pattern']}/"
            elif ctype == "length":
                n = len(body)
                lo, hi = spec.get("min", 0), spec.get("max", 10 ** 9)
                ok = lo <= n <= hi
                detail = f"{n}文字(範囲 {lo}〜{hi})"
            elif ctype == "html_runtime":
                if shutil.which("node") is None:
                    ok, detail = True, "スキップ(node が無い)"
                else:
                    err = await tb.verify_runtime("html")
                    ok = err is None
                    detail = "起動時エラーなし" if ok else err
            elif ctype == "js_syntax":
                warn = await js_syntax_check(artifact_name, artifact_text or "")
                ok = warn is None
                detail = "構文OK" if ok else warn
            elif ctype == "run":
                out = await tb.run_command(spec["command"], ".", spec.get("timeout", 60))
                m = re.match(r"exit=(-?\d+)\n--- stdout ---\n(.*)\n--- stderr ---\n(.*)", out, re.S)
                if not m:
                    detail = out[:300]           # [拒否]/[timeout]/[実行エラー]
                else:
                    code, stdout, stderr = int(m.group(1)), m.group(2), m.group(3)
                    pat = spec.get("expect_stdout_regex")
                    matched = re.search(pat, stdout, re.S) is not None if pat else True
                    ok = code == 0 and matched
                    detail = (f"exit={code}" + ("" if matched else " 出力が期待と不一致")
                              + (f" stderr: {stderr.strip()[:200]}" if stderr.strip() else ""))
            elif ctype == "json_valid":
                data = json.loads(artifact_text or "")
                missing = [k for k in spec.get("keys", []) if not (isinstance(data, dict) and k in data)]
                mismatch = [k for k, v in (spec.get("equals") or {}).items()
                            if not (isinstance(data, dict) and data.get(k) == v)]
                ok = not missing and not mismatch
                detail = ("JSON妥当" if ok else
                          " / ".join(filter(None, [
                              f"欠損キー: {missing}" if missing else "",
                              f"値の不一致: {mismatch}" if mismatch else ""])))
            else:
                detail = f"未知のチェック種別: {ctype}"
        except Exception as e:   # チェック自体の失敗は「不合格+理由」として残す(ベンチは止めない)
            ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
        results.append({"type": ctype, "ok": ok, "detail": str(detail)[:240]})
    return results


# ============================================================ 集計 ----
def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 2) if xs else None


def _stdev(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.stdev(xs), 2) if len(xs) >= 2 else None


def _judge_avg(j: dict | None) -> float | None:
    if not j or j.get("scores") is None:
        return None
    return round(statistics.fmean(j["scores"][k] for k in JUDGE_CRITERIA), 2)


def summarize_task(t: dict) -> dict:
    """課題1件の反復(reps)を平均して UI の表1行ぶんにまとめる。"""
    reps = [r for r in t["reps"] if r.get("timing")]
    timings = [r["timing"] for r in reps]
    checks = [c for r in t["reps"] for c in (r.get("checks") or [])]
    judges = [_judge_avg(r.get("judge")) for r in t["reps"]]
    scores = {k: _mean([r["judge"]["scores"][k] for r in t["reps"]
                        if r.get("judge") and r["judge"].get("scores")]) for k in JUDGE_CRITERIA}
    passed, judge_avg = sum(1 for c in checks if c["ok"]), _mean(judges)
    return {
        "tok_per_s": _mean([x.get("tok_per_s") for x in timings]),
        "ttft_ms": _mean([x.get("ttft_ms") for x in timings]),
        "load_ms": _mean([x.get("load_ms") for x in timings]),
        "eval_tokens": _mean([x.get("eval_tokens") for x in timings]),
        "wall_ms": _mean([x.get("wall_ms") for x in timings]),
        "checks_passed": passed,
        "checks_total": len(checks),
        "judge_avg": judge_avg,
        "judge_scores": scores if any(v is not None for v in scores.values()) else None,
        # 自動チェックが落ちているのに採点が高い = judge が成果物内の文言に釣られた疑い
        # (プロンプトインジェクション/甘い採点)。UI で警告を出す
        "suspicious": bool(checks and passed < len(checks) and judge_avg is not None and judge_avg >= 4),
        "truncated": sum(1 for r in t["reps"] if r.get("truncated")),
        "errors": sum(1 for r in t["reps"] if r.get("error")),
        "timing_missing": sum(1 for r in t["reps"] if r.get("timing_missing")),
    }


def aggregate(bench: dict) -> dict:
    """Run全体の2軸サマリー(速度 / 品質)。UIの比較表はこれだけを読む。"""
    reps = [r for t in bench["tasks"] for r in t["reps"]]
    timings = [r["timing"] for r in reps if r.get("timing")]
    # ウォーム TTFT: ロードが乗っていない反復だけ(初回課題はコールドロードを含む)
    warm = [x["ttft_ms"] for x in timings if (x.get("load_ms") or 0) < 1000]
    checks = [c for r in reps for c in (r.get("checks") or [])]
    judges = [_judge_avg(r.get("judge")) for r in reps]
    judge_avg = _mean(judges)
    pass_rate = round(sum(1 for c in checks if c["ok"]) / len(checks), 3) if checks else None
    parts = [p for p in (pass_rate * 100 if pass_rate is not None else None,
                         judge_avg / 5 * 100 if judge_avg is not None else None) if p is not None]
    tps = [x.get("tok_per_s") for x in timings]
    return {
        "tok_per_s": _mean(tps),
        "tok_per_s_sd": _stdev(tps),
        "ttft_ms": _mean([x["ttft_ms"] for x in timings]),
        "warm_ttft_ms": _mean(warm),
        "max_load_ms": max((x.get("load_ms") or 0 for x in timings), default=None),
        "eval_tokens": sum(x.get("eval_tokens") or 0 for x in timings),
        "gen_wall_s": round(sum(x.get("wall_ms") or 0 for x in timings) / 1000, 1),
        "check_pass_rate": pass_rate,
        "checks_passed": sum(1 for c in checks if c["ok"]),
        "checks_total": len(checks),
        "judge_avg": judge_avg,
        "quality_score": round(statistics.fmean(parts), 1) if parts else None,
        "truncated": sum(1 for r in reps if r.get("truncated")),
        "errors": sum(1 for r in reps if r.get("error")),
        "reps_done": len(timings),
        "reps_total": len(reps),
    }


# ============================================================ orchestrator ----
def _fmt_timing(x: dict) -> str:
    if not x:
        # Ollama が done チャンクを返さずにストリームを閉じた(プロセス再起動等)。
        # 生成物は得られているので、速度だけ「不明」として扱う
        return "速度データなし(done チャンク未受信: Ollama 側でストリームが途切れた可能性)"
    tps = f"{x['tok_per_s']:.1f} tok/s" if x.get("tok_per_s") else "tok/s 不明"
    return (f"{tps} / 初トークン {x.get('ttft_ms', 0) / 1000:.1f}s (ロード {x.get('load_ms', 0) / 1000:.1f}s) / "
            f"{x.get('eval_tokens', 0)} tokens / 所要 {x.get('wall_ms', 0) / 1000:.1f}s")


def _clip(text: str, cap: int = ARTIFACT_JUDGE_CAP) -> str:
    if len(text) <= cap:
        return text
    head, tail = cap * 2 // 3, cap // 3
    return f"{text[:head]}\n…(中略 {len(text) - cap}文字)…\n{text[-tail:]}"


class BenchOrchestrator:
    """1ベンチRunの全工程。run.bench を逐次更新し、EventBus にノードを流す。"""

    def __init__(self, run, cfg, suite: dict, tasks: list[dict], repeats: int,
                 judge_model: str | None, checkpoint=None):
        self.run = run
        self.bus = run.bus
        self.cfg = cfg
        self.suite = suite
        self.tasks = tasks
        self.repeats = max(1, int(repeats))
        self.judge_model = judge_model
        self.checkpoint = checkpoint   # 課題完了ごとの中間保存(async callable / None)
        self.model = run.model
        self.info = llm.resolve(cfg, run.model)
        self.root_dir = f"bench_{run.id}"
        # 成果物本文は judge に渡すためだけに保持し、永続化対象の run.bench には載せない
        self._texts: dict[tuple[str, int], str | None] = {}

    # ---- 記録 ----
    def _init_record(self) -> dict:
        rec = {
            "suite_version": self.suite.get("version"),
            "model": self.model, "model_tag": self.info["tag"],
            "placement": self.info["placement"], "think": self.info.get("think"),
            "num_ctx": self.info["num_ctx"],
            "judge_model": self.judge_model,
            "judge_tag": llm.resolve(self.cfg, self.judge_model)["tag"] if self.judge_model else None,
            "repeats": self.repeats,
            "task_ids": [t["id"] for t in self.tasks],
            "phase": "generate",
            "tasks": [{
                "id": t["id"], "title": t["title"], "category": t.get("category", ""),
                "kind": t.get("kind", "text"),
                "reps": [{"rep": i + 1, "timing": None, "checks": None, "judge": None,
                          "artifact": None, "error": None, "truncated": False}
                         for i in range(self.repeats)],
                "summary": None,
            } for t in self.tasks],
            "aggregate": None,
        }
        self.run.bench = rec
        return rec

    async def _refresh(self) -> None:
        rec = self.run.bench
        for t in rec["tasks"]:
            t["summary"] = summarize_task(t)
        rec["aggregate"] = aggregate(rec)
        if self.checkpoint:
            await self.checkpoint()

    def _publish_artifacts(self) -> None:
        from orchestrator import _collect_files
        from tools import WORKSPACE
        root = os.path.join(WORKSPACE, self.root_dir)
        if os.path.isdir(root):
            _, arts = _collect_files(root, cap_bytes=1)
            for a in arts:
                a.pop("_prio", None)
            self.bus.set_artifacts(arts)

    # ---- フェーズ1: 生成 ----
    async def _generate(self, task: dict, rep: dict, node_id: str) -> dict:
        bus = self.bus
        messages = [{"role": "system", "content": self.suite.get("system", "").strip()},
                    {"role": "user", "content": task["prompt"].strip()}]
        bus.set_prompt(node_id, "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages))
        bus.set_status(node_id, "thinking")

        num_predict = task.get("num_predict") or self.suite.get("num_predict")
        msg = await llm.chat(self.cfg, self.model, messages, num_predict=num_predict,
                             on_delta=_bus_delta(bus, node_id), retries=1)
        meta = llm.strip_meta(msg)
        timing = meta.get("_timing") or {}
        if timing.get("eval_tokens"):
            bus.set_tokens(node_id, timing["eval_tokens"])
        # timing は「計測できた反復」だけに入れる(None なら集計から自然に外れる)
        rep["timing"] = timing or None
        rep["timing_missing"] = not timing
        rep["truncated"] = meta.get("_done_reason") == "length"
        rep["thinking_chars"] = len(meta.get("_thinking") or "")

        # 成果物を書き出し(生応答も残す)、決定論チェックを回す。
        # 無人実行なので秘密情報を剥いだ環境で子プロセスを動かす
        tb = Toolbox(subdir=f"{self.root_dir}/{task['id']}/r{rep['rep']}", approve=False,
                     env=_safe_env())
        tb.write_file("response.md", msg.get("content") or "")
        if meta.get("_thinking"):
            tb.write_file("thinking.md", meta["_thinking"])
        name, body = extract_artifact(task, msg.get("content") or "")
        if name:
            tb.write_file(name, body)
            rep["artifact"] = {"name": name,
                               "path": f"/projects/{self.root_dir}/{task['id']}/r{rep['rep']}/{name}",
                               "chars": len(body)}
        self._texts[(task["id"], rep["rep"])] = body
        bus.set_status(node_id, "running")   # チェック実行中(GPUは解放)
        rep["checks"] = await run_checks(task, tb, name, body, msg.get("content") or "")
        return timing

    async def _phase_generate(self, root_id: str) -> None:
        bus, rec = self.bus, self.run.bench
        step, total = 0, len(self.tasks) * self.repeats * (2 if self.judge_model else 1)
        for t_idx, task in enumerate(self.tasks):
            for rep in rec["tasks"][t_idx]["reps"]:
                suffix = f" #{rep['rep']}" if self.repeats > 1 else ""
                node_id = bus.create_node("author", f"🧪 {task['title']}{suffix}",
                                          f"モデル: {self.info['tag']} / {task.get('category', '')}",
                                          root_id)
                rep["node_id"] = node_id
                try:
                    timing = await self._generate(task, rep, node_id)
                    checks = rep["checks"] or []
                    passed = sum(1 for c in checks if c["ok"])
                    lines = [_fmt_timing(timing)]
                    if rep["truncated"]:
                        lines.append("⚠ 生成が長さ上限で打ち切られました")
                    if not rep["artifact"]:
                        lines.append("⚠ 成果物を抽出できませんでした")
                    for c in checks:
                        lines.append(f"{'✅' if c['ok'] else '❌'} {c['type']}: {c['detail']}")
                    tail = f" — チェック {passed}/{len(checks)}" if checks else ""
                    bus.set_title(node_id, f"🧪 {task['title']}{suffix}{tail}")
                    bus.complete(node_id, "\n".join(lines))
                except asyncio.CancelledError:
                    raise
                except Exception as e:   # Ollama障害・抽出失敗は課題単位で記録して次へ
                    rep["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                    bus.complete(node_id, error=rep["error"])
                step += 1
                bus.set_progress(root_id, step, total)
                self._publish_artifacts()
                await self._refresh()

    # ---- フェーズ3: judge ----
    async def _judge_one(self, task: dict, rep: dict, node_id: str) -> dict:
        bus = self.bus
        checks = rep.get("checks") or []
        check_lines = "\n".join(f"- {c['type']}: {'合格' if c['ok'] else '不合格'} ({c['detail']})"
                                for c in checks) or "- (自動チェックなし)"
        artifact = self._texts.get((task["id"], rep["rep"]))
        body = _clip(artifact) if artifact else "(成果物を抽出できませんでした)"
        user = (f"# 課題: {task['title']}\n{task['prompt'].strip()}\n\n"
                f"# この課題で特に見る点\n{(task.get('rubric') or '').strip()}\n\n"
                f"# 自動チェックの結果\n{check_lines}\n\n"
                f"# 生成された成果物({rep['artifact']['name'] if rep.get('artifact') else '本文'})\n"
                f"{body}")
        messages = [{"role": "system", "content": self.suite.get("judge_system", "").strip()},
                    {"role": "user", "content": user}]
        bus.set_prompt(node_id, "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages))
        bus.set_status(node_id, "thinking")
        on_delta = _bus_delta(bus, node_id)
        raw, result = "", None
        for attempt in range(2):   # _review_json と同じ「1回だけ再要求」方式
            msg = await llm.chat(self.cfg, self.judge_model, messages, json_schema=JUDGE_SCHEMA,
                                 temperature=0.1, num_predict=4000, on_delta=on_delta, retries=1)
            meta = llm.strip_meta(msg)
            if meta.get("_usage"):
                bus.set_tokens(node_id, meta["_usage"])
            raw = _strip_think(msg.get("content") or "")
            try:
                data = json.loads(_balanced_json(raw) or raw)
                scores = {k: int(data[k]) for k in JUDGE_CRITERIA}
                if all(1 <= v <= 5 for v in scores.values()):
                    result = {"scores": scores, "comment": str(data.get("comment", ""))[:400]}
                    break
            except (ValueError, TypeError, KeyError):
                pass
            messages = messages + [msg, {"role": "user", "content":
                                         "JSON形式が不正でした。requirements/correctness/quality(1〜5の整数)と comment だけを持つJSONを出力してください。"}]
        if result is None:
            result = {"scores": None, "comment": f"採点JSONを解釈できませんでした: {raw[:300]}"}
        result["model"] = self.judge_model
        return result

    async def _phase_judge(self, root_id: str) -> None:
        bus, rec = self.bus, self.run.bench
        rec["phase"] = "judge"
        total = len(self.tasks) * self.repeats * 2
        step = len(self.tasks) * self.repeats
        for t_idx, task in enumerate(self.tasks):
            for rep in rec["tasks"][t_idx]["reps"]:
                step += 1
                if rep.get("error"):
                    bus.set_progress(root_id, step, total)
                    continue
                suffix = f" #{rep['rep']}" if self.repeats > 1 else ""
                node_id = bus.create_node("reviewer", f"🔍 品質評価: {task['title']}{suffix}",
                                          f"judge: {rec['judge_tag']}", root_id)
                try:
                    rep["judge"] = await self._judge_one(task, rep, node_id)
                    j = rep["judge"]
                    if j["scores"]:
                        s = j["scores"]
                        head = (f"要件 {s['requirements']}/5 · 正確性 {s['correctness']}/5 · "
                                f"完成度 {s['quality']}/5 (平均 {_judge_avg(j):.1f})")
                        bus.set_title(node_id, f"🔍 {task['title']}{suffix} — {_judge_avg(j):.1f}/5")
                        bus.complete(node_id, f"{head}\n{j['comment']}")
                    else:
                        bus.complete(node_id, j["comment"])
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    rep["judge"] = {"scores": None, "comment": f"採点失敗: {e}", "model": self.judge_model}
                    bus.complete(node_id, error=f"採点失敗: {type(e).__name__}: {str(e)[:300]}")
                bus.set_progress(root_id, step, total)
                await self._refresh()

    # ---- まとめ ----
    def _report(self) -> str:
        rec = self.run.bench
        agg = rec["aggregate"] or {}
        f = lambda v, fmt: (fmt % v) if v is not None else "—"
        lines = [f"# ベンチ結果: {rec['model_tag']}",
                 f"judge: {rec['judge_tag'] or 'なし(決定論チェックのみ)'} / 反復 {rec['repeats']} / "
                 f"課題 {len(rec['tasks'])} / 生成合計 {agg.get('gen_wall_s', 0)}s", "",
                 "## 速度",
                 f"- 生成速度: {f(agg.get('tok_per_s'), '%.1f')} tok/s"
                 + (f" (±{agg['tok_per_s_sd']})" if agg.get("tok_per_s_sd") else ""),
                 f"- 初トークン(全体平均): {f(agg.get('ttft_ms'), '%.0f')} ms / "
                 f"ウォーム: {f(agg.get('warm_ttft_ms'), '%.0f')} ms / 最大ロード: {f(agg.get('max_load_ms'), '%.0f')} ms",
                 f"- 生成トークン合計: {agg.get('eval_tokens', 0)}", "",
                 "## 品質",
                 f"- 自動チェック: {agg.get('checks_passed', 0)}/{agg.get('checks_total', 0)}",
                 f"- LLM採点平均: {f(agg.get('judge_avg'), '%.2f')}/5",
                 f"- 品質スコア: {f(agg.get('quality_score'), '%.0f')}/100", "",
                 "| 課題 | tok/s | TTFT | チェック | 採点 |", "|---|---|---|---|---|"]
        for t in rec["tasks"]:
            s = t["summary"] or {}
            lines.append(f"| {t['title']} | {f(s.get('tok_per_s'), '%.1f')} | "
                         f"{f(s.get('ttft_ms'), '%.0f')}ms | {s.get('checks_passed', 0)}/{s.get('checks_total', 0)} | "
                         f"{f(s.get('judge_avg'), '%.1f')} |")
        if agg.get("truncated"):
            lines.append(f"\n⚠ 長さ上限で打ち切られた生成: {agg['truncated']}件")
        if agg.get("errors"):
            lines.append(f"⚠ エラー: {agg['errors']}件")
        lines.append("\n※単発生成のベンチです。エージェント実運用(ツールループ)の完走性能は別途実測で判断してください。")
        return "\n".join(lines)

    async def run_bench(self) -> None:
        """メインエントリ。(run は Run オブジェクトの属性名なので run_task 同様に別名)"""
        bus = self.bus
        bus.reset()
        rec = self._init_record()
        root_id = bus.create_node(
            "task", f"ベンチマーク: {self.info['tag']}",
            f"課題 {len(self.tasks)}件 × {self.repeats}回 / judge: {rec['judge_tag'] or 'なし'}")
        bus.set_status(root_id, "running")
        bus.set_progress(root_id, 0, len(self.tasks) * self.repeats * (2 if self.judge_model else 1))
        t0 = time.time()
        try:
            await self._phase_generate(root_id)
            if self.judge_model and any(not r.get("error") for t in rec["tasks"] for r in t["reps"]):
                await self._phase_judge(root_id)
            rec["phase"] = "done"
            rec["elapsed_s"] = round(time.time() - t0, 1)
            await self._refresh()
            answer_id = bus.create_node("answer", "📊 ベンチ結果", "", root_id)
            bus.complete(answer_id, self._report())
            bus.complete(root_id, f"ベンチ完了({rec['elapsed_s']}s)")
            bus.run_finished()
        except asyncio.CancelledError:
            # 集計だけ更新して保存は manager の最終 persist に任せる(キャンセル中に await しない)
            rec["phase"] = "cancelled"
            for t in rec["tasks"]:
                t["summary"] = summarize_task(t)
            rec["aggregate"] = aggregate(rec)
            bus.cancel_all("ユーザーによって中断されました")
            raise
        except Exception as e:
            rec["phase"] = "error"
            for t in rec["tasks"]:
                t["summary"] = summarize_task(t)
            rec["aggregate"] = aggregate(rec)
            bus.complete(root_id, error=f"ベンチ実行エラー: {e}")
            bus.run_finished(error=str(e))


def bench_config(rec: dict | None) -> dict | None:
    """再実行用: 記録から起動パラメータだけを取り出す。"""
    if not rec:
        return None
    return {"model": rec["model"], "tasks": list(rec.get("task_ids") or []),
            "repeats": rec.get("repeats", 1), "judge": rec.get("judge_model") or "none"}

