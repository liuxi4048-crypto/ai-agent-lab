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

import bench
import claude_review
import llm
import router
from artifacts import WORKSPACE as ARTIFACT_DIR
from orchestrator import (CodeOrchestrator, CritiqueOrchestrator, Orchestrator,
                          SwarmCodeOrchestrator)
from runs import manager, normalize_snapshot
from tools import WORKSPACE as PROJECTS_DIR

app = FastAPI(title="AI Agent Lab")

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
app.mount("/workspace", StaticFiles(directory=ARTIFACT_DIR), name="workspace")
app.mount("/projects", StaticFiles(directory=PROJECTS_DIR), name="projects")
if (STATIC_REACT / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_REACT / "assets"), name="assets")

MODES = ("orchestra", "critique", "code", "swarm-code")
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
    claude_review: bool = False    # 完了後にClaude(外部API)がレビュー→修正して仕上げる


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
    }


@app.get("/claude")
async def claude() -> dict:
    """Claudeレビュー(外部API)が使えるか。UIのトグル可否と注意表示に使う。"""
    return claude_review.status()


@app.get("/models")
async def models() -> dict:
    cfg = llm.load_config()
    catalog = llm.model_catalog(cfg)
    installed = set(await llm.list_models())

    def _found(tag: str) -> bool:
        return tag in installed or f"{tag}:latest" in installed

    free_ram = llm.free_ram_gb()
    for m in catalog["models"]:
        m["installed"] = _found(m["tag"])
        # RAM併用が必要なモデルは、空きRAMが足りるかも返す(UIで注意表示に使う)
        m["needs_ram"] = m["placement"] == "hybrid"
        m["ram_ok"] = (free_ram is None or not m.get("ram_gb")
                       or m["ram_gb"] <= free_ram)
    catalog["free_ram_gb"] = round(free_ram, 1) if free_ram is not None else None
    catalog["claude"] = claude_review.status()
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
    if not await llm.is_alive():
        raise HTTPException(503, "Ollamaが起動していません(ollama serve を実行してください)")
    if req.claude_review:
        # 成果物のソースを外部(Anthropic)へ送る工程。使えない状態なら黙って
        # スキップせず、理由を返して気づけるようにする
        st = claude_review.status()
        if not st["available"]:
            raise HTTPException(400, f"Claudeレビューを使えません: {st['reason']}")

    cfg = llm.load_config()
    installed = set(await llm.list_models())

    # --- 進め方と成果物形式をメインエージェントに決めさせる -------------------
    decided_by = "user"
    reason = ""
    mode, deliverable = req.mode, req.deliverable
    if mode == "auto":
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

    # 明示指定モデルの存在検証(typo等は実行途中に落ちるより先に弾く)
    if req.model and req.model != "auto" and not _installed(cfg, model, installed):
        raise HTTPException(
            400, f"モデル '{llm.resolve(cfg, model)['tag']}' はOllamaに存在しません。"
                 f"`ollama pull {llm.resolve(cfg, model)['tag']}` で導入してください")

    # 明示指定のモデルがRAMを食い過ぎる場合は、黙って遅くならないよう先に弾く
    chosen = llm.resolve(cfg, model)
    if (ram_msg := _ram_error(chosen, free_ram)):
        raise HTTPException(400, ram_msg)

    reviewer = None
    if mode == "critique" or (mode == "code" and req.critique):
        reviewer = req.reviewer_model or router.critique_pair(cfg, model)

    info = llm.resolve(cfg, model)
    if mode in ("code", "swarm-code") and not info["tools"]:
        raise HTTPException(400, f"モデル '{model}' は tools 非対応のため {mode} で使えません")

    hybrid = _is_hybrid(cfg, model, reviewer or "")
    # Claudeレビューは成果物ファイルを直接直す工程なので、ファイルを作るモードのみ
    claude_on = req.claude_review and mode in ("code", "swarm-code")
    max_iter = max(1, min(int(req.max_iter or 18), 200))
    run = manager.create(task, mode, model, reviewer,
                         approve=req.approve, max_iter=max_iter, hybrid=hybrid,
                         critique=req.critique, deliverable=deliverable,
                         claude_review=claude_on)

    def factory():
        if run.mode == "critique":
            return CritiqueOrchestrator(run.bus, cfg, run.model, run.reviewer_model).run(task, run.id)
        if run.mode == "code":
            return CodeOrchestrator(run, cfg, critique=req.critique,
                                    reviewer_model=run.reviewer_model).run_task(task)
        if run.mode == "swarm-code":
            return SwarmCodeOrchestrator(run, cfg, worker_model=run.model).run_task(task)
        return Orchestrator(run.bus, cfg, run.model).run(task, run.id)

    manager.start(run, factory)
    return {"status": "started", "run_id": run.id, "model": model,
            "model_tag": info["tag"], "mode": mode, "deliverable": deliverable,
            "reviewer_model": reviewer, "hybrid": hybrid,
            "claude_review": claude_on,
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
    if not await llm.is_alive():
        raise HTTPException(503, "Ollamaが起動していません(ollama serve を実行してください)")
    cfg = llm.load_config()
    if req.model not in cfg.get("models", {}):
        raise HTTPException(400, f"未知のモデルキー: {req.model}")
    if not 1 <= req.repeats <= 5:
        raise HTTPException(400, "repeats は 1〜5")
    installed = set(await llm.list_models())
    info = llm.resolve(cfg, req.model)
    if not _installed(cfg, req.model, installed):
        raise HTTPException(400, f"モデル {info['tag']} は未導入です(ollama pull {info['tag']})")
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
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
