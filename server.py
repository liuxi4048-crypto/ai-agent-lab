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
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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


def _is_hybrid(cfg, *keys: str) -> bool:
    return any(llm.resolve(cfg, k)["placement"] == "hybrid" for k in keys if k)


def _installed(cfg, key: str, installed: set) -> bool:
    tag = llm.resolve(cfg, key)["tag"]
    return tag in installed or f"{tag}:latest" in installed


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
    return {"ollama": await llm.is_alive(), "running": manager.running_count()}


@app.get("/models")
async def models() -> dict:
    cfg = llm.load_config()
    catalog = llm.model_catalog(cfg)
    installed = set(await llm.list_models())

    def _found(tag: str) -> bool:
        return tag in installed or f"{tag}:latest" in installed

    for m in catalog["models"]:
        m["installed"] = _found(m["tag"])
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

    model = (req.model if req.model and req.model != "auto"
             else router.pick_model(cfg, task, mode, installed=installed))

    reviewer = None
    if mode == "critique" or (mode == "code" and req.critique):
        reviewer = req.reviewer_model or router.critique_pair(cfg, model)

    info = llm.resolve(cfg, model)
    if mode in ("code", "swarm-code") and not info["tools"]:
        raise HTTPException(400, f"モデル '{model}' は tools 非対応のため {mode} で使えません")

    # 成果物形式: コード生成モードでのみ意味を持つ(orchestra/critiqueは文章生成)
    if mode in ("code", "swarm-code"):
        if deliverable in (None, "auto"):
            deliverable = router.pick_deliverable(task)
    else:
        deliverable = None

    hybrid = _is_hybrid(cfg, model, reviewer or "")
    run = manager.create(task, mode, model, reviewer,
                         approve=req.approve, max_iter=req.max_iter, hybrid=hybrid,
                         critique=req.critique, deliverable=deliverable)

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
            "decided_by": decided_by, "reason": reason}


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
        raise HTTPException(404, "継続可能なRunが見つかりません(codeモードの完了済みタスクのみ対応)")
    cfg = llm.load_config()

    def factory():
        return CodeOrchestrator(run, cfg, critique=run.critique,
                                reviewer_model=run.reviewer_model).continue_task(message)

    manager.start(run, factory)
    return {"status": "started", "run_id": run.id}


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
        snapshot["resumable"] = (mode == "code")

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
