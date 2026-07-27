"""Run管理: 複数タスクの並列実行・キューイング・保存・一覧。

agent-orchestra からの移植 + 拡張:
- 409で弾く代わりに asyncio.Semaphore でキューイング(queued状態で待機→自動開始)
- placement=hybrid(VRAM+RAMオフロードの大型モデル)の Run は専用ロックで直列強制
- run_command の実行前承認 Future を Run 単位で管理(キャンセル時は一括却下)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from artifacts import _BASE
from events import EventBus

RUNS_DIR = _BASE / "runs"

MAX_CONCURRENT = 3
APPROVAL_TIMEOUT = 900   # 承認待ちの上限秒。放置時は自動却下(スロット飢餓の防止)
CHECKPOINT_SEC = 10      # 実行中Runの定期スナップショット間隔(プロセス死対策)


class Run:
    def __init__(self, task: str, mode: str, model: str,
                 reviewer_model: str | None = None, approve: bool = True,
                 max_iter: int = 18, hybrid: bool = False):
        self.id = uuid.uuid4().hex[:10]
        self.task = task
        self.mode = mode                  # orchestra / critique / code / swarm-code
        self.model = model                # models.yaml のキー
        self.reviewer_model = reviewer_model
        self.approve = approve
        self.max_iter = max_iter
        self.hybrid = hybrid              # True なら直列強制
        self.bus = EventBus()
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.cancelled = False
        self.queued = True
        self.task_obj: asyncio.Task | None = None
        self.pending_approvals: dict[str, asyncio.Future] = {}

    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.finished_at is not None:  # persist済み=終了が最優先の事実
            return "error" if self.error else "done"
        if self.queued:
            return "queued"
        return "running"

    def summary(self) -> dict:
        root = next((self.bus.nodes[i] for i in self.bus.order
                     if self.bus.nodes[i].kind == "task"), None)
        return {
            "id": self.id,
            "task": self.task,
            "model": self.model,
            "mode": self.mode,
            "reviewer_model": self.reviewer_model,
            "hybrid": self.hybrid,
            "status": self.status(),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "progress": list(root.progress) if root and root.progress else None,
            "tokens": sum(self.bus.nodes[i].tokens for i in self.bus.order),
        }

    # ---- 承認フロー ----
    async def wait_approval(self, node_id: str, command: str, cwd: str) -> bool:
        aid = uuid.uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending_approvals[aid] = fut
        self.bus.request_approval(aid, node_id, command, cwd)
        try:
            return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            # 承認放置でスロット/hybridロックを永久占有しない。UIのカードも閉じる
            self.bus.emit_log(node_id,
                              f"[承認タイムアウト] {APPROVAL_TIMEOUT}s 応答なし → 自動却下: {command}")
            self.bus.resolve_approval(aid, False)
            return False
        finally:
            self.pending_approvals.pop(aid, None)

    def resolve_approval(self, aid: str, approved: bool) -> bool:
        fut = self.pending_approvals.get(aid)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        self.bus.resolve_approval(aid, approved)
        return True

    def reject_pending(self) -> None:
        """キャンセル/終了時: 保留中の承認をすべて却下してデッドロックを防ぐ。"""
        for aid, fut in list(self.pending_approvals.items()):
            if not fut.done():
                fut.set_result(False)
            self.bus.resolve_approval(aid, False)
        self.pending_approvals.clear()


class RunManager:
    def __init__(self) -> None:
        self.live: dict[str, Run] = {}
        self.slots = asyncio.Semaphore(MAX_CONCURRENT)
        self.hybrid_lock = asyncio.Lock()

    def running_count(self) -> int:
        return sum(1 for r in self.live.values() if r.status() == "running")

    def create(self, task: str, mode: str, model: str,
               reviewer_model: str | None = None, approve: bool = True,
               max_iter: int = 18, hybrid: bool = False) -> Run:
        run = Run(task, mode, model, reviewer_model, approve, max_iter, hybrid)
        self.live[run.id] = run
        return run

    def start(self, run: Run, factory) -> None:
        """factory() -> coroutine(オーケストレーター本体)をゲート付きで起動する。"""

        async def _checkpoint() -> None:
            # プロセス死(ウィンドウクローズ・taskkill・電源断)でも直近状態が残るように
            while True:
                await asyncio.sleep(CHECKPOINT_SEC)
                self.persist(run, final=False)

        async def _gated() -> None:
            ck = asyncio.create_task(_checkpoint())
            try:
                if run.hybrid:
                    # hybridロックを先に取る(ロック待ちで並列スロットを浪費しない)
                    async with self.hybrid_lock:
                        async with self.slots:
                            run.queued = False
                            await factory()
                else:
                    async with self.slots:
                        run.queued = False
                        await factory()
            except asyncio.CancelledError:
                run.error = run.error or "ユーザーによって中断されました"
            except Exception as e:
                run.error = str(e)
            finally:
                ck.cancel()
                run.reject_pending()
                self.persist(run)
                self.live.pop(run.id, None)  # 以後は runs/*.json から配信(メモリ解放)

        run.task_obj = asyncio.create_task(_gated())

    def persist(self, run: Run, final: bool = True) -> None:
        """Runの状態をディスクへ書く。final=False はチェックポイント(実行中のまま)。

        tmp+replace の原子的書き込みにする(書き込み途中のクラッシュで
        JSONが壊れると _load_file が黙って捨て「Run消失」が再発するため)。
        """
        if final:
            run.finished_at = time.time()
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        data = {**run.summary(), "snapshot": run.bus.full_snapshot()}
        tmp = RUNS_DIR / f"{run.id}.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RUNS_DIR / f"{run.id}.json")

    def _load_file(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def list_runs(self) -> list[dict]:
        items = [r.summary() for r in self.live.values()]
        seen = {r["id"] for r in items}
        if RUNS_DIR.is_dir():
            for path in RUNS_DIR.glob("*.json"):
                data = self._load_file(path)
                if data and data["id"] not in seen:
                    items.append({k: v for k, v in data.items() if k != "snapshot"})
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items

    def get_snapshot(self, run_id: str) -> dict | None:
        """ライブ実行ならその場のスナップショット、終了済みなら保存済みを返す。"""
        if run_id in self.live:
            return self.live[run_id].bus.full_snapshot()
        data = self._load_file(RUNS_DIR / f"{run_id}.json")
        return data.get("snapshot") if data else None

    def get_live(self, run_id: str) -> Run | None:
        return self.live.get(run_id)

    def recover_interrupted(self) -> int:
        """起動時: 前回プロセス死で終了記録のないRunを interrupted として確定する。"""
        n = 0
        if not RUNS_DIR.is_dir():
            return 0
        for path in RUNS_DIR.glob("*.json"):
            data = self._load_file(path)
            if not data or data.get("finished_at") is not None:
                continue
            data["status"] = "interrupted"
            data["finished_at"] = path.stat().st_mtime
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            n += 1
        return n

    def cancel(self, run_id: str) -> bool:
        """実行中/待機中のRunを中断する。対象がない/既に終了していればFalse。"""
        run = self.live.get(run_id)
        if run is None or run.task_obj is None or run.task_obj.done():
            return False
        run.cancelled = True
        run.reject_pending()
        run.task_obj.cancel()
        return True


manager = RunManager()
