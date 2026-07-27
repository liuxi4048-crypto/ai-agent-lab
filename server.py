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
from runs import manager
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
ARTIFACT_DIR.mkdir(exist_ok=True)
Path(PROJECTS_DIR).mkdir(exist_ok=True)
app.mount("/workspace", StaticFiles(directory=ARTIFACT_DIR), name="workspace")
app.mount("/projects", StaticFiles(directory=PROJECTS_DIR), name="projects")

MODES = ("orchestra", "critique", "code", "swarm-code")


class RunRequest(BaseModel):
    task: str
    mode: str = "code"
    model: str = "auto"            # models.yaml のキー or "auto"
    reviewer_model: str = ""       # critique / code+critique 用。空なら異ファミリー自動選定
    approve: bool = True           # run_command の実行前承認(code / swarm-code)
    critique: bool = False         # code モード: 完走後にレビュー→FIXラウンド
    max_iter: int = 18


def _is_hybrid(cfg, *keys: str) -> bool:
    return any(llm.resolve(cfg, k)["placement"] == "hybrid" for k in keys if k)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


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
    if req.mode not in MODES:
        raise HTTPException(400, f"mode は {MODES} のいずれか")
    if not await llm.is_alive():
        raise HTTPException(503, "Ollamaが起動していません(ollama serve を実行してください)")

    cfg = llm.load_config()
    installed = set(await llm.list_models())
    model = (req.model if req.model and req.model != "auto"
             else router.pick_model(cfg, task, req.mode, installed=installed))

    reviewer = None
    if req.mode == "critique" or (req.mode == "code" and req.critique):
        reviewer = req.reviewer_model or router.critique_pair(cfg, model)

    info = llm.resolve(cfg, model)
    if req.mode in ("code", "swarm-code") and not info["tools"]:
        raise HTTPException(400, f"モデル '{model}' は tools 非対応のため {req.mode} で使えません")

    hybrid = _is_hybrid(cfg, model, reviewer or "")
    run = manager.create(task, req.mode, model, reviewer,
                         approve=req.approve, max_iter=req.max_iter, hybrid=hybrid)

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
            "reviewer_model": reviewer, "hybrid": hybrid}


@app.post("/run/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    if not manager.cancel(run_id):
        raise HTTPException(409, "実行中のタスクが見つからないか、既に終了しています")
    return {"status": "cancelling"}


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


@app.get("/events/{run_id}")
async def events(run_id: str) -> StreamingResponse:
    live = manager.get_live(run_id)
    bus = live.bus if live else None

    if bus is None:
        # 終了・保存済みRun: スナップショットを1回送って保持
        snapshot = manager.get_snapshot(run_id)
        if snapshot is None:
            raise HTTPException(404, "指定されたタスクが見つかりません")

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
            yield f"data: {json.dumps(bus.full_snapshot(), ensure_ascii=False)}\n\n"
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
