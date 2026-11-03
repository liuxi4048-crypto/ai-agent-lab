"""FastAPI サーバー: ダッシュボード配信 + マルチタスク実行API + SSE。

agent-orchestra の server.py を土台に移植・拡張:
- モード: orchestra / critique / code / swarm-code
- モデルは models.yaml のキー("auto" はヒューリスティックルーティング)
- MAX_CONCURRENT 超過は 409 ではなくキューイング(queued → 自動開始)
- placement=hybrid のモデルを使う Run は直列強制(runs.py の hybrid_lock)
- POST /run/{id}/approvals/{aid}: run_command の実行前承認への応答

起動: python server.py  →  http://127.0.0.1:8765
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import answer_cache
import bench
import openai_api
import claude_review
import api_review
import llm
import router
from artifacts import WORKSPACE as ARTIFACT_DIR
from orchestrator import (CascadeOrchestrator, CodeOrchestrator, CritiqueOrchestrator,
                          DirectOrchestrator, Orchestrator, SwarmCodeOrchestrator)
from runs import manager, normalize_snapshot
from tools import WORKSPACE as PROJECTS_DIR

app = FastAPI(title="AI Agent Lab")

# OpenAI互換API (/v1/models, /v1/chat/completions) — openai_api.py 参照
openai_api.register(app)


@app.on_event("startup")
async def _startup_prewarm():
    # サーバ起動直後に default モデルを先読みし、その日の初回タスクの
    # ロード待ち(17〜65秒)をなくす。無効化は環境変数 AAL_NO_PREWARM=1。
    import os
    if os.environ.get("AAL_NO_PREWARM"):
        return
    asyncio.create_task(llm.prewarm_default())
    # cascade 高速モデルの keep-warm: 期限切れ・追い出しで Ollama が
    # 空になったときだけ温め直し、次の cascade の下書きが再ロード待ち
    # (実測最大約120秒) で止まらないようにする。他モデルが載っている
    # ときは追い出さない (llm.keepwarm_should_load)。
    asyncio.create_task(llm.keepwarm_fast_model(manager.running_count))


# 起動時: 前回プロセス死で実行中のまま残ったRun記録を interrupted として確定
try:
    _n = manager.recover_interrupted()
    if _n:
        print(f"[recover] 前回中断のRun {_n}件を interrupted として確定")
except OSError as _e:
    print(f"[recover] 失敗(継続): {_e}")

STATIC = Path(__file__).parent / "static"
STATIC_REACT = Path(__file__).parent / "static-react"  # 新UI(frontend/ のビルド成果物)
ARTIFACT_DIR.mkdir(exist_ok=True)
Path(PROJECTS_DIR).mkdir(exist_ok=True)

# JS/CSS を gzip 圧縮して配信する(無圧縮313KB→約95KB。2026-10-27監査)
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1024)


class _ImmutableStatic(StaticFiles):
    """ハッシュ付きファイル名の資産用: 再検証なしで長期キャッシュさせる。"""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


app.mount("/workspace", StaticFiles(directory=ARTIFACT_DIR), name="workspace")
app.mount("/projects", StaticFiles(directory=PROJECTS_DIR), name="projects")
if (STATIC_REACT / "assets").is_dir():
    app.mount("/assets", _ImmutableStatic(directory=STATIC_REACT / "assets"), name="assets")
# インサイト: Claude Code セッションのダッシュボード(insight/claude.py が runs/ 配下に書き出す)
INSIGHT_CC = Path(__file__).parent / "runs" / "insight-cache" / "cc"
INSIGHT_CC.mkdir(parents=True, exist_ok=True)
app.mount("/insight-cc", StaticFiles(directory=INSIGHT_CC), name="insight-cc")

MODES = ("orchestra", "critique", "code", "swarm-code", "direct")
DELIVERABLES = ("auto", "html", "exe", "script")
# 仕分けに使う高速モデル(VRAM常駐・tools対応)。無ければ default にフォールバック
TRIAGE_PREFERRED = ("worker", "coder", "smart")


class RunRequest(BaseModel):
    task: str
    mode: str = "auto"             # auto = メインエージェントが進め方を決める
    model: str = "auto"            # models.yaml のキー or "auto"
    reviewer_model: str = ""       # critique / code+critique 用。空なら異ファミリー自動選定
    approve: bool = True           # run_command の実行前承認(code / swarm-code)
    critique: bool = False         # code モード: 完走後にレビュー→FIXラウンド
    max_iter: int = 18
    deliverable: str = "auto"      # 成果物形式 auto / html / exe / script
    allow_ram: bool = False        # True でRAM併用の大型モデルも使う(既定はVRAMのみ)
    claude_review: bool = False
    api_review: bool = False      # 完了後に環境変数設定済みの任意APIモデルがレビュー
    temperature: float | None = None   # 生成温度の Run 単位上書き (未指定ならモデル既定)
    max_tokens: int | None = None      # 生成上限トークンの Run 単位上書き (同上)
    think: bool | None = None          # 思考生成の Run 単位指定 (None=モデル既定, True=あり, False=なし)


def _is_hybrid(cfg, *keys: str) -> bool:
    return any(llm.resolve(cfg, k)["placement"] == "hybrid" for k in keys if k)


def _installed(cfg, key: str, installed: set) -> bool:
    tag = llm.resolve(cfg, key)["tag"]
    return tag in installed or f"{tag}:latest" in installed


def _ram_error(info: dict, free_ram: float | None) -> str | None:
    """RAM併用モデルが空きRAMに収まらないときの理由文(収まるなら None)。

    黙って遅くなる(ページング)より先に弾くための共通チェック(/run と /bench)。
    """
    if info["placement"] == "hybrid" and info["ram_gb"] and free_ram is not None \
            and info["ram_gb"] > free_ram:
        return (f"'{info['tag']}' は約{info['ram_gb']}GBのRAMが必要ですが、"
                f"空きは{free_ram:.1f}GBです。他のアプリを閉じるか、VRAMに収まる"
                "モデルを選んでください")
    return None


@app.get("/")
async def index() -> FileResponse:
    """新UI(React)がビルド済みならそちらを配信。/legacy で旧UIも残す。"""
    new_ui = STATIC_REACT / "index.html"
    return FileResponse(new_ui if new_ui.is_file() else STATIC / "index.html")


@app.get("/legacy")
async def legacy() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/gpu")
async def gpu() -> dict:
    return await llm.gpu_status()


@app.get("/health")
async def health() -> dict:
    from runs import MAX_CONCURRENT
    running = manager.running_count()
    return {
        "ollama": await llm.is_alive(),
        "running": running,
        "slots_free": max(0, MAX_CONCURRENT - running),
        # 検証レイヤの死活(黙って無効化されている状態をUIで認知できるように)
        "node_check": shutil.which("node") is not None,
        "claude_review": claude_review.status().get("available", False),
        "api_review": api_review.status().get("available", False),
    }


@app.get("/claude")
async def claude() -> dict:
    """Claudeレビュー(外部API)が使えるか。UIのトグル可否と注意表示に使う。"""
    return claude_review.status()


@app.get("/models")
async def models() -> dict:
    cfg = llm.load_config()
    models_list, alive = await llm.list_models_with_status()   # /api/tags は1回だけ
    installed = set(models_list)
    catalog = llm.model_catalog(cfg, installed)

    def _found(tag: str) -> bool:
        return tag in installed or f"{tag}:latest" in installed

    free_ram = llm.free_ram_gb()
    for m in catalog["models"]:
        # RAM併用が必要なモデルは、空きRAMが足りるかも返す(UIで注意表示に使う)
        m["needs_ram"] = m["placement"] == "hybrid"
        m["ram_ok"] = (free_ram is None or not m.get("ram_gb")
                       or m["ram_gb"] <= free_ram)
    # 使えるか(installed)はバックエンドごとに判定する(Ollama=導入済み、
    # OpenAI互換=サーバー応答とモデル有無、Anthropic=キー有無)。
    # backend_status に理由も付け、UIが状態表示に使う。
    probes: dict = {}
    await llm.annotate_models(cfg, catalog["models"], installed, probes)
    # 接続状態パネル用: 接続先単位の到達可否と公開モデル一覧
    catalog["connections"] = llm.summarize_connections(
        catalog["models"], installed, alive, probes)
    catalog["free_ram_gb"] = round(free_ram, 1) if free_ram is not None else None
    catalog["claude"] = claude_review.status()
    catalog["api_review"] = api_review.status()
    return catalog


@app.get("/runs")
async def list_runs() -> dict:
    return {"runs": manager.list_runs()}


@app.post("/run")
async def start_run(req: RunRequest) -> dict:
    task = req.task.strip()
    if not task:
        raise HTTPException(400, "task が空です")
    if req.mode not in MODES and req.mode != "auto":
        raise HTTPException(400, f"mode は 'auto' か {MODES} のいずれか")
    if req.deliverable not in DELIVERABLES:
        raise HTTPException(400, f"deliverable は {DELIVERABLES} のいずれか")
    if req.temperature is not None and not 0.0 <= req.temperature <= 2.0:
        raise HTTPException(400, "temperature は 0.0〜2.0 の範囲で指定してください")
    if req.max_tokens is not None and not 1 <= req.max_tokens <= 65536:
        raise HTTPException(400, "max_tokens は 1〜65536 の範囲で指定してください")
    gen_override: dict = {}
    if req.temperature is not None:
        gen_override["temperature"] = req.temperature
    if req.max_tokens is not None:
        gen_override["num_predict"] = req.max_tokens
    if req.think is not None:
        gen_override["think"] = req.think
    if not await llm.is_alive():
        raise HTTPException(503, "Ollamaが起動していません(ollama serve を実行してください)")
    if req.claude_review:
        # 成果物のソースを外部(Anthropic)へ送る工程。使えない状態なら黙って
        # スキップせず、理由を返して気づけるようにする
        st = claude_review.status()
        if not st["available"]:
            raise HTTPException(400, f"Claudeレビューを使えません: {st['reason']}")
    if req.api_review and not api_review.status()["available"]:
        raise HTTPException(400, f"任意モデルレビューを使えません: {api_review.status()['reason']}")

    cfg = llm.load_config()
    installed = set(await llm.list_models())

    # --- 進め方と成果物形式をメインエージェントに決めさせる -------------------
    # モデル明示指定 (pinned) のときは OpenAI / Anthropic のモデル選択と同じく
    # そのモデルの単独実行にし、他モデルによる仕分け自体をしない。
    pinned = router.is_pinned_model(req.model)
    decided_by = "user"
    reason = ""
    mode, deliverable = req.mode, req.deliverable
    if pinned and mode == "auto":
        mode = router.pinned_mode(mode)
        decided_by, reason = "pinned", "モデル明示指定のため単独実行"
    elif mode == "auto":
        triage_key = next((k for k in TRIAGE_PREFERRED
                           if k in cfg.get("models", {})
                           and _installed(cfg, k, installed)), cfg.get("default", "coder"))
        picked = await router.triage(cfg, task, triage_key)
        mode = picked["mode"]
        decided_by, reason = picked["by"], picked["reason"]
        if deliverable == "auto":
            deliverable = picked["deliverable"] or "auto"

    # 成果物形式: コード生成モードでのみ意味を持つ(orchestra/critiqueは文章生成)
    if mode in ("code", "swarm-code"):
        if deliverable in (None, "auto"):
            deliverable = router.pick_deliverable(task)
    else:
        deliverable = None

    # 単一HTML形式は「index.html 全部入り」なので並列分解と相性が悪い
    # (各サブが完全なアプリを別々に作る)。自動判定時は code へ降格する。
    if mode == "swarm-code" and deliverable == "html" and decided_by != "user":
        mode = "code"
        reason = (reason + " / " if reason else "") + "単一HTMLのため並列分解を回避"

    free_ram = llm.free_ram_gb()
    model = (req.model if req.model and req.model != "auto"
             else router.pick_model(cfg, task, mode, installed=installed,
                                    allow_ram=req.allow_ram, free_ram_gb=free_ram))

    # 明示指定モデルの利用可否を検証(typo等は実行途中に落ちるより先に弾く)。
    # 判定と理由文はバックエンドごと(llm.model_usable / unusable_reason)。
    if req.model and req.model != "auto":
        usable, status = await llm.model_usable(cfg, model, installed)
        if not usable:
            raise HTTPException(400, llm.unusable_reason(llm.resolve(cfg, model), status))

    # 明示指定のモデルがRAMを食い過ぎる場合は、黙って遅くならないよう先に弾く
    chosen = llm.resolve(cfg, model)
    if (ram_msg := _ram_error(chosen, free_ram)):
        raise HTTPException(400, ram_msg)

    reviewer = None
    if mode == "critique" or (mode == "code" and req.critique):
        reviewer = router.pick_reviewer(cfg, model, req.reviewer_model, pinned)

    info = llm.resolve(cfg, model)
    if mode in ("code", "swarm-code") and not info["tools"]:
        raise HTTPException(400, f"モデル '{model}' は tools 非対応のため {mode} で使えません")

    # direct 適格でも実行モデルが遅い高性能モデルのときは、高速モデル先行の
    # カスケードへ格上げする (cascade_fast_model が有効で実行モデルと違う場合)。
    # ただしモデル明示指定時は格上げしない (選んだモデル自身が書く)。
    mode = router.maybe_cascade(mode, model, cfg, pinned)

    hybrid = _is_hybrid(cfg, model, reviewer or "")
    # Claudeレビューは成果物ファイルを直接直す工程なので、ファイルを作るモードのみ
    claude_on = req.claude_review and mode in ("code", "swarm-code")
    max_iter = max(1, min(int(req.max_iter or 18), 200))
    run = manager.create(task, mode, model, reviewer,
                         approve=req.approve, max_iter=max_iter, hybrid=hybrid,
                         critique=req.critique, deliverable=deliverable,
                         claude_review=claude_on,
                         api_review=req.api_review and mode in ("code", "swarm-code"),
                         gen_override=gen_override, pinned=pinned)

    def factory():
        if run.mode == "cascade":
            fast = cfg.get("cascade_fast_model")
            if fast not in cfg.get("models", {}):
                fast = None
            if fast and fast != run.model:
                coro = CascadeOrchestrator(run.bus, cfg, run.model,
                                           fast, gen_override=run.gen_override).run(task, run.id)
            else:
                coro = DirectOrchestrator(run.bus, cfg, run.model,
                                          gen_override=run.gen_override).run(task, run.id)
        elif run.mode == "direct":
            coro = DirectOrchestrator(run.bus, cfg, run.model,
                                      gen_override=run.gen_override).run(task, run.id)
        elif run.mode == "critique":
            coro = CritiqueOrchestrator(run.bus, cfg, run.model, run.reviewer_model).run(task, run.id)
        elif run.mode == "code":
            coro = CodeOrchestrator(run, cfg, critique=req.critique,
                                    reviewer_model=run.reviewer_model).run_task(task)
        elif run.mode == "swarm-code":
            coro = SwarmCodeOrchestrator(run, cfg, worker_model=run.model).run_task(task)
        else:
            # orchestra の分解だけ小型高速モデルへ振り分ける(第4弾)。models.yaml の
            # orchestra_planner_model が未設定/未知キーなら実行モデルが分解も行う。
            # モデル明示指定時は差し替えない (router.orchestra_planner)。
            planner_model = router.orchestra_planner(cfg, run.pinned)
            coro = Orchestrator(run.bus, cfg, run.model,
                                planner_model=planner_model,
                                gen_override=run.gen_override).run(task, run.id)
        # 同一タスクの再実行キャッシュ (文章系モードのみ、answer_cache 参照)。
        # 生成パラメータ指定付きの Run はキャッシュ対象外 (キーが task/model/mode のみで、
        # パラメータ違いの回答が混ざるのを防ぐ)。モデル明示指定時も対象外:
        # 公式のモデル選択と同じく、選んだモデルが毎回その場で生成する。
        if run.gen_override or run.pinned:
            return coro
        return answer_cache.wrap(run, task, coro)

    manager.start(run, factory)
    return {"status": "started", "run_id": run.id, "model": model,
            "model_tag": info["tag"], "mode": mode, "deliverable": deliverable,
            "reviewer_model": reviewer, "hybrid": hybrid, "pinned": pinned,
            "claude_review": claude_on,
            "api_review": run.api_review,
            "decided_by": decided_by, "reason": reason}


# ============================================================ bench ----
class BenchRequest(BaseModel):
    model: str                     # models.yaml のキー(tier ゲートなし: 退役/保留モデルの再ベンチ用)
    tasks: list[str] = []          # bench_suite.yaml の課題ID。空なら全課題
    repeats: int = 1               # 各課題の反復回数(速度のばらつきを見るとき 2〜5)
    judge: str = "auto"            # LLM採点モデル: auto(異ファミリー自動) / none / キー


@app.get("/bench/suite")
async def bench_suite() -> dict:
    try:
        return bench.suite_catalog(bench.load_suite())
    except (OSError, ValueError, KeyError) as e:
        raise HTTPException(500, f"bench_suite.yaml を読めません: {e}")


@app.post("/bench")
async def start_bench(req: BenchRequest) -> dict:
    """固定プロンプト集を1モデルに投げる性能ベンチ。mode="bench" の Run として走る。

    並列スロット・hybrid直列化・永続化・SSE・UIの履歴は通常Runと共通。
    """
    cfg = llm.load_config()
    if req.model not in cfg.get("models", {}):
        raise HTTPException(400, f"未知のモデルキー: {req.model}")
    if not 1 <= req.repeats <= 5:
        raise HTTPException(400, "repeats は 1〜5")
    installed = set(await llm.list_models())
    info = llm.resolve(cfg, req.model)
    # Ollama の死活は Ollama バックエンドのモデルをベンチするときだけ要求する
    if info.get("backend", "ollama") == "ollama" and not await llm.is_alive():
        raise HTTPException(503, "Ollamaが起動していません(ollama serve を実行してください)")
    usable, status = await llm.model_usable(cfg, req.model, installed)
    if not usable:
        raise HTTPException(400, llm.unusable_reason(info, status))
    if (ram_msg := _ram_error(info, llm.free_ram_gb())):
        raise HTTPException(400, ram_msg)
    try:
        suite = bench.load_suite()
        tasks = bench.select_tasks(suite, req.tasks)
        judge = bench.pick_judge(cfg, req.model, req.judge, installed)
    except (OSError, ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    if len(tasks) * req.repeats > bench.MAX_REPS_PER_RUN:
        raise HTTPException(400, f"課題数×反復は {bench.MAX_REPS_PER_RUN} 以下にしてください")

    # ベンチは常に hybrid 扱い(hybrid_lock を占有)にする。OLLAMA_MAX_LOADED_MODELS=1 では
    # 他Runとモデルを取り合うだけで速度計測が汚れるため、少なくとも大型モデルのRunとは重ねない
    label = f"ベンチ: {info['tag']} ({len(tasks)}課題" + (f"×{req.repeats}" if req.repeats > 1 else "") + ")"
    run = manager.create(label, "bench", req.model, reviewer_model=judge, approve=False,
                         max_iter=0, hybrid=True)

    async def checkpoint():
        # 課題完了ごとの中間保存。定期チェックポイントと同じく、書き込みはスレッドへ逃がす
        data = manager._encode(run, final=False)
        await asyncio.to_thread(manager._write_record, run, data, False)

    orch = bench.BenchOrchestrator(run, cfg, suite, tasks, req.repeats, judge, checkpoint=checkpoint)
    manager.start(run, orch.run_bench)
    return {"status": "started", "run_id": run.id, "model": req.model,
            "model_tag": info["tag"], "mode": "bench", "judge": judge,
            "judge_tag": llm.resolve(cfg, judge)["tag"] if judge else None,
            "tasks": [t["id"] for t in tasks], "repeats": req.repeats,
            "hybrid": _is_hybrid(cfg, req.model, judge or "")}


@app.post("/run/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    if not manager.cancel(run_id):
        raise HTTPException(409, "実行中のタスクが見つからないか、既に終了しています")
    return {"status": "cancelling"}


@app.delete("/run/{run_id}")
async def delete_run(run_id: str) -> dict:
    result = manager.delete(run_id)
    if result == "running":
        raise HTTPException(409, "実行中のタスクは削除できません(先に中断してください)")
    if result == "not_found":
        raise HTTPException(404, "指定されたタスクが見つかりません")
    return {"status": "deleted"}


class ApprovalRequest(BaseModel):
    approved: bool


@app.post("/run/{run_id}/approvals/{aid}")
async def resolve_approval(run_id: str, aid: str, req: ApprovalRequest) -> dict:
    run = manager.get_live(run_id)
    if run is None:
        raise HTTPException(404, "指定されたタスクが見つかりません")
    if not run.resolve_approval(aid, req.approved):
        raise HTTPException(409, "その承認要求は存在しないか、既に応答済みです")
    return {"status": "ok"}


class ContinueRequest(BaseModel):
    message: str


@app.post("/run/{run_id}/continue")
async def continue_run(run_id: str, req: ContinueRequest) -> dict:
    """完了済みcodeモードRunに追加指示を送り、同じ会話・同じワークスペースで継続する。

    成果物の修正・追加機能の指示などに使う。継続の度に同じRun記録(同じ run_id)へ
    ノードが積み重なり、runs/{run_id}.json も上書き更新される。
    """
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "message が空です")
    if manager.get_live(run_id) is not None:
        raise HTTPException(409, "実行中のタスクには追加指示を送れません(完了後に送信してください)")
    if not await llm.is_alive():
        raise HTTPException(503, "Ollamaが起動していません(ollama serve を実行してください)")

    run = manager.reopen(run_id)
    if run is None:
        raise HTTPException(404, "継続可能なRunが見つかりません(完了済みのタスクを選んでください)")
    try:
        cfg = llm.load_config()
        # 継続作業はツールを使うため、tools非対応モデルのRunはコーダーへ切り替える
        if not llm.resolve(cfg, run.model)["tools"]:
            installed = set(await llm.list_models())
            run.model = router.pick_model(cfg, message, "code", installed=installed)
            run.model_tag = llm.resolve(cfg, run.model)["tag"]

        def factory():
            return CodeOrchestrator(run, cfg, critique=run.critique,
                                    reviewer_model=run.reviewer_model).continue_task(message)

        manager.start(run, factory)
    except Exception:
        # start 前に失敗すると task_obj=None のゾンビが live に残留し、中断も削除も
        # 継続もできなくなる(サーバー再起動まで詰まる)。必ず除去してから再raise。
        manager.live.pop(run.id, None)
        raise
    return {"status": "started", "run_id": run.id}


@app.get("/run/{run_id}")
async def get_run(run_id: str) -> dict:
    """Run 1件のレコード(summary+正規化済みsnapshot)をJSONで返す。

    完了済みRunの閲覧・外部ツール(CLI/スクリプト)からの結果取得用。
    SSE接続(/events)を張らずに済む。
    """
    live = manager.get_live(run_id)
    if live is not None:
        return {**live.summary(), "snapshot": live.bus.full_snapshot()}
    record = manager.get_record(run_id)
    if record is None:
        raise HTTPException(404, "指定されたタスクが見つかりません")
    if record.get("snapshot") is not None:
        record["snapshot"] = normalize_snapshot(record["snapshot"],
                                                record.get("status", "done"))
    return record


# ---- インサイト(実行記録の可視化と分析エージェント) ----
class ExplainRequest(BaseModel):
    source: str              # "run" = ai-agent-lab の Run / "claude" = Claude Code のセッション
    id: str
    model: str = "worker"


_json_file_cache: dict = {}


def _read_json_cached(path: Path):
    """静的JSONの読み込みキャッシュ(mtime+size が変わったときだけ読み直す)。"""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _json_file_cache:
        _json_file_cache.clear()
        _json_file_cache[key] = json.loads(path.read_text(encoding="utf-8"))
    return _json_file_cache[key]


@app.get("/insight/glossary")
def insight_glossary():
    base = _read_json_cached(Path(__file__).parent / "insight" / "cclab" / "data" / "glossary.json")
    local = _read_json_cached(Path(__file__).parent / "insight" / "glossary_local.json")
    return {"categories": local["categories"] + base["categories"], "terms": local["terms"] + base["terms"]}


@app.get("/insight/runs")
def insight_runs():
    from insight import agentlab
    return {"runs": agentlab.list_runs()}


@app.get("/insight/run/{run_id}")
def insight_run(run_id: str):
    from insight import agentlab
    try:
        return agentlab.load_run(run_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "Run が見つかりません")


@app.get("/insight/claude/sessions")
def insight_claude_sessions():
    from insight import claude
    return {"available": claude.available(), "sessions": claude.list_sessions()}


@app.post("/insight/claude/{sid}/build")
async def insight_claude_build(sid: str):
    from insight import claude
    try:
        # 数 MB の JSONL を読むので、イベントループを止めないよう別スレッドで作る
        return await asyncio.to_thread(claude.build, sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "セッションが見つかりません")


@app.post("/insight/explain")
async def insight_explain(req: ExplainRequest):
    from insight import agentlab, analyst, claude
    try:
        if req.source == "run":
            facts = agentlab.facts_for_analyst(agentlab.load_run(req.id))
        elif req.source == "claude":
            facts = await asyncio.to_thread(claude.facts_for_analyst, req.id)
        else:
            raise HTTPException(400, "source は run か claude")
        return await analyst.explain(facts, req.model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "記録が見つかりません")
    except KeyError as e:
        raise HTTPException(400, f"models.yaml に無いモデルです: {e}")
    except llm.OllamaError as e:
        raise HTTPException(503, f"ローカルLLMに接続できません: {e}")


@app.get("/events/{run_id}")
async def events(run_id: str) -> StreamingResponse:
    live = manager.get_live(run_id)
    bus = live.bus if live else None
    mode = live.mode if live else None

    if bus is None:
        # 終了・保存済みRun: スナップショットを1回送って保持
        record = manager.get_record(run_id)
        if record is None or record.get("snapshot") is None:
            raise HTTPException(404, "指定されたタスクが見つかりません")
        mode = record.get("mode")
        snapshot = normalize_snapshot(record["snapshot"], record.get("status", "done"))
        snapshot["mode"] = mode
        snapshot["resumable"] = True  # 完了済みRunはどのモードでも会話を続けられる

        async def replay():
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            while True:
                await asyncio.sleep(15)
                yield ": keepalive\n\n"

        return StreamingResponse(replay(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    q = bus.subscribe()

    async def stream():
        try:
            first = bus.full_snapshot()
            first["mode"] = mode
            first["resumable"] = False  # ライブ中(実行中/待機中)は継続不可
            yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    # バインド先は AAL_HOST / api_settings.json で変更可 (既定は従来どおり 127.0.0.1)
    uvicorn.run(app, host=openai_api.bind_host(), port=8765, log_level="warning")
