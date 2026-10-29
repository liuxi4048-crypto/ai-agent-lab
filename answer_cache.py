"""同一タスクの再実行キャッシュ (2026-10-28 追加)。

task・モデル・モードが完全一致する再実行では、保存済みの最終回答を
即座に返す。エンジン側でも完全一致プロンプトは状態再利用で高速に
なるが (実測 TTFT 0.32 秒)、アプリ側で返せば生成そのものが不要になる。
対象は副作用のない文章系モード (direct / cascade / orchestra) のみで、
成果物を作る code 系や推敲は対象外。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent / "answer_cache.json"
MAX_ENTRIES = 200
ELIGIBLE_MODES = {"direct", "cascade", "orchestra"}


def cache_key(task: str, model: str, mode: str) -> str:
    norm = " ".join((task or "").split())
    return hashlib.sha256(f"{mode}\n{model}\n{norm}".encode("utf-8")).hexdigest()


def _load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get(task: str, model: str, mode: str) -> dict | None:
    return _load().get(cache_key(task, model, mode))


def put(task: str, model: str, mode: str, answer: str, run_id: str = "") -> None:
    if not answer:
        return
    data = _load()
    data[cache_key(task, model, mode)] = {
        "task": task, "model": model, "mode": mode, "answer": answer,
        "run_id": run_id, "created_at": time.time(),
    }
    if len(data) > MAX_ENTRIES:
        oldest = sorted(data.items(),
                        key=lambda kv: kv[1].get("created_at", 0))
        for k, _ in oldest[: len(data) - MAX_ENTRIES]:
            del data[k]
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE_PATH)


async def wrap(run, task: str, coro) -> None:
    """factory のコルーチンを包む: ヒット時は即返し、ミス時は完了後に保存する。"""
    bus = run.bus
    if run.mode not in ELIGIBLE_MODES:
        await coro
        return
    hit = get(task, run.model, run.mode)
    if hit:
        bus.reset()
        root_id = bus.create_node("task", "ユーザータスク", task)
        bus.set_status(root_id, "running")
        answer_id = bus.create_node("answer", "最終回答", "", root_id)
        bus.emit_log(answer_id,
                     "[キャッシュ] 同一タスクの保存済み回答を返しました "
                     f"(保存元 Run: {hit.get('run_id', '?')})")
        bus.complete(answer_id, hit["answer"])
        bus.complete(root_id, "全工程が完了しました")
        bus.run_finished()
        return
    await coro
    snap = bus.full_snapshot()
    for n in snap.get("nodes", []):
        if n.get("kind") == "answer" and n.get("output"):
            put(task, run.model, run.mode, n["output"], run.id)
            break
