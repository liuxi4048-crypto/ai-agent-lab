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


def normalize_snapshot(snapshot: dict, run_status: str) -> dict:
    """保存済み(終了/中断)Runのsnapshotを事実と一致させる。

    チェックポイントは running:true・ノードが thinking/generating/running のまま
    保存されることがある(プロセス死・中断)。/events の再生表示と、継続実行のための
    EventBus.from_snapshot 復元の両方で使う共通の正規化。
    """
    snapshot = dict(snapshot)
    snapshot["running"] = False
    snapshot["run_status"] = run_status
    nodes = []
    for n in snapshot.get("nodes", []):
        n = dict(n)
        if n.get("status") not in ("done", "error", "cancelled"):
            n["status"] = "cancelled"
        nodes.append(n)
    snapshot["nodes"] = nodes
    return snapshot


class Run:
    def __init__(self, task: str, mode: str, model: str,
                 reviewer_model: str | None = None, approve: bool = True,
                 max_iter: int = 18, hybrid: bool = False, critique: bool = False,
                 deliverable: str | None = None, claude_review: bool = False):
        self.id = uuid.uuid4().hex[:10]
        self.task = task
        self.mode = mode                  # orchestra / critique / code / swarm-code
        self.model = model                # models.yaml のキー
        self.reviewer_model = reviewer_model
        self.approve = approve
        self.max_iter = max_iter
        self.hybrid = hybrid              # True なら直列強制
        self.critique = critique          # codeモード: レビュー+FIXラウンド
        self.deliverable = deliverable    # 成果物形式 html / exe / script
        # 完了後にClaude(外部API)がレビュー→修正して最終成果物に仕上げる。
        # ソースを外部送信するため既定OFF・Runごとの明示的なONでのみ有効。
        self.claude_review = claude_review
        # 表示用の実モデル名(qwen3:30b 等)。キーだけだと何のモデルか分からないため
        try:
            import llm as _llm
            self.model_tag = _llm.resolve(_llm.load_config(), model)["tag"]
        except Exception:
            self.model_tag = model
        self.bus = EventBus()
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.cancelled = False
        self.queued = True
        self.task_obj: asyncio.Task | None = None
        self.pending_approvals: dict[str, asyncio.Future] = {}
        self.history: list = []  # codeモード: 会話継続用の最終メッセージ列

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
        # 「今どのエージェントが何をしているか」をカードに出すための現在ステップ
        step = None
        for i in reversed(self.bus.order):
            n = self.bus.nodes[i]
            if n.kind != "task" and n.status in ("thinking", "generating", "running"):
                step = n.title
                break
        return {
            "current_step": step,
            "id": self.id,
            "task": self.task,
            "model": self.model,
            "model_tag": self.model_tag,
            "mode": self.mode,
            "reviewer_model": self.reviewer_model,
            "hybrid": self.hybrid,
            "approve": self.approve,
            "critique": self.critique,
            "claude_review": self.claude_review,
            "deliverable": self.deliverable,
            "max_iter": self.max_iter,
            "status": self.status(),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "progress": list(root.progress) if root and root.progress else None,
            "tokens": sum(self.bus.nodes[i].tokens for i in self.bus.order),
            "pending_approvals": len(self.pending_approvals),
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
               max_iter: int = 18, hybrid: bool = False, critique: bool = False,
               deliverable: str | None = None, claude_review: bool = False) -> Run:
        run = Run(task, mode, model, reviewer_model, approve, max_iter, hybrid,
                  critique, deliverable, claude_review)
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
        data = {**run.summary(), "snapshot": run.bus.full_snapshot(), "history": run.history}
        tmp = RUNS_DIR / f"{run.id}.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RUNS_DIR / f"{run.id}.json")

    def _load_file(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _fill_tag(rec: dict) -> dict:
        """model_tag 導入前に保存されたRunにも実モデル名を補う。"""
        if not rec.get("model_tag") and rec.get("model"):
            try:
                import llm as _llm
                rec["model_tag"] = _llm.resolve(_llm.load_config(), rec["model"])["tag"]
            except Exception:
                rec["model_tag"] = rec["model"]
        return rec

    def list_runs(self) -> list[dict]:
        items = []
        for r in self.live.values():
            s = r.summary()
            if s["status"] == "queued":
                s["queue_reason"] = (
                    "hybrid直列待ち(大型モデルの同時実行を防止)"
                    if r.hybrid and self.hybrid_lock.locked()
                    else f"並列上限{MAX_CONCURRENT}件に到達")
            items.append(s)
        seen = {r["id"] for r in items}
        if RUNS_DIR.is_dir():
            for path in RUNS_DIR.glob("*.json"):
                data = self._load_file(path)
                if data and data["id"] not in seen:
                    items.append(self._fill_tag(
                        {k: v for k, v in data.items() if k != "snapshot"}))
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items

    def get_snapshot(self, run_id: str) -> dict | None:
        """ライブ実行ならその場のスナップショット、終了済みなら保存済みを返す。"""
        if run_id in self.live:
            return self.live[run_id].bus.full_snapshot()
        record = self.get_record(run_id)
        return record.get("snapshot") if record else None

    def get_record(self, run_id: str) -> dict | None:
        """保存済みRunのレコード全体(summary+snapshot)を返す。"""
        return self._load_file(RUNS_DIR / f"{run_id}.json")

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

    def reopen(self, run_id: str) -> Run | None:
        """完了済みRunをディスクから復元し、続きの会話を実行できる状態にする。

        実行中/待機中(self.live に存在)なら None(呼び出し側で判定・拒否)。
        会話履歴を持つ(code/swarm-code)ならその続きとして、持たない
        (orchestra/critique)なら同じワークスペースで新たにコーディングエージェントを
        走らせる形で継続する。中断・失敗ケースの履歴末尾に未応答の tool_calls が
        残っていれば合成応答を補ってから復元する(次のターンが壊れないように)。
        """
        if run_id in self.live:
            return None
        data = self.get_record(run_id)
        if data is None or data.get("snapshot") is None:
            return None
        run = Run(data["task"], data["mode"], data["model"], data.get("reviewer_model"),
                  data.get("approve", True), data.get("max_iter", 18),
                  data.get("hybrid", False), data.get("critique", False),
                  data.get("deliverable"), data.get("claude_review", False))
        run.id = run_id
        run.created_at = data.get("created_at", run.created_at)
        from agent import _patch_dangling_tool_calls
        run.history = _patch_dangling_tool_calls(data.get("history") or [])
        snapshot = normalize_snapshot(data["snapshot"], data.get("status", "done"))
        run.bus = EventBus.from_snapshot(snapshot)
        self.live[run.id] = run
        return run

    def delete(self, run_id: str) -> str:
        """終了済みRunの記録を削除する。返り値: ok / running / not_found。"""
        if run_id in self.live:
            return "running"
        path = RUNS_DIR / f"{run_id}.json"
        if not path.is_file():
            return "not_found"
        path.unlink()
        return "ok"


manager = RunManager()
