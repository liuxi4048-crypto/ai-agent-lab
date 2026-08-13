"""イベントバス: オーケストレーターのノード状態を保持し、SSE購読者へ配信する。

agent-orchestra からの移植 + 拡張:
- emit_log(node_id, line): コーディングエージェントの逐次ログ(log_line イベント)
- request_approval / resolve_approval: run_command の実行前承認フロー
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

LOG_CAP = 2000    # ノードあたりの保持ログ行数上限
THINK_CAP = 20000  # ノードあたりの思考テキスト保持上限(文字)
OUTPUT_CAP = 400000  # ノード出力の保持上限(文字)。無制限成長でsnapshot/persistが肥大するのを防ぐ
FLUSH_INTERVAL = 0.1  # token/think ストリームのSSEフラッシュ間隔(秒)


class Node:
    def __init__(self, node_id: str, parent_id: str | None, kind: str, title: str, detail: str = ""):
        self.id = node_id
        self.parent_id = parent_id
        self.kind = kind          # task / planner / subagent / aggregator / answer / coder / round / author / reviewer / merger
        self.title = title
        self.detail = detail      # 作業内容の説明
        self.status = "waiting"   # waiting / thinking / generating / running / done / error / cancelled
        self.tokens = 0
        self.preview = ""         # 生成中テキストの末尾プレビュー
        self.output = ""          # 完全な出力
        self.think = ""           # thinking(推論)テキスト。UIでは折りたたみ表示
        self.prompt = ""          # 与えたプロンプト(ログ表示用)
        self.log: list[str] = []  # coder ノードの逐次ログ
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.progress: tuple[int, int] | None = None  # (完了, 総数) 親ノード用
        self.ctx_fill: float | None = None  # コンテキスト充填率(coderノード)

    def snapshot(self) -> dict[str, Any]:
        # log はコピーを返す(persist がワーカースレッドで json.dumps する間に
        # イベントループ側が append すると "changed size during iteration" になるため)
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
            "tokens": self.tokens,
            "preview": self.preview,
            "output": self.output,
            "think": self.think,
            "prompt": self.prompt,
            "log": list(self.log),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": list(self.progress) if self.progress else None,
            "ctx_fill": self.ctx_fill,
        }


class EventBus:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.order: list[str] = []  # 作成順(上から下への表示順)
        self.run_started_at: float | None = None
        self.running = False
        self.artifacts: list[dict] = []
        self.approvals: dict[str, dict] = {}  # aid -> {node_id, command, cwd, resolved, approved}
        self._subscribers: list[asyncio.Queue] = []
        # token/think ストリームのコアレッシング(トークン毎に1イベント発行すると
        # 50tok/s×並列3Runで毎秒150イベントになり、UI再描画と回線を圧迫するため)
        self._tok_dirty: set[str] = set()
        self._think_buf: dict[str, list[str]] = {}
        self._flush_handle = None

    # ---- 購読 ----
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _publish(self, event: dict) -> None:
        for q in self._subscribers:
            q.put_nowait(event)

    def full_snapshot(self) -> dict:
        """スナップショット全量。必ずイベントループスレッド上で呼ぶこと
        (返り値はコピーなので、その後のシリアライズはスレッドに逃がしてよい)。"""
        return {
            "type": "snapshot",
            "running": self.running,
            "run_started_at": self.run_started_at,
            "nodes": [self.nodes[i].snapshot() for i in self.order],
            "artifacts": list(self.artifacts),
            "approvals": [dict(a) for a in self.approvals.values() if not a["resolved"]],
        }

    # ---- 復元(会話継続用) ----
    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "EventBus":
        """保存済みsnapshot(正規化済み)からノード木を復元する。

        継続実行(continue_task)は既存の木にノードを追加していくため、
        bus.reset()せずここから作る。running は呼び出し側が明示的に立てる。
        """
        bus = cls()
        bus.run_started_at = snapshot.get("run_started_at")
        bus.artifacts = list(snapshot.get("artifacts") or [])
        for nd in snapshot.get("nodes", []):
            node = Node(nd["id"], nd.get("parent_id"), nd["kind"], nd["title"], nd.get("detail", ""))
            node.status = nd.get("status", "done")
            node.tokens = nd.get("tokens", 0)
            node.preview = nd.get("preview", "")
            node.output = nd.get("output", "")
            node.think = nd.get("think", "")
            node.prompt = nd.get("prompt", "")
            node.log = list(nd.get("log") or [])
            node.started_at = nd.get("started_at")
            node.finished_at = nd.get("finished_at")
            p = nd.get("progress")
            node.progress = tuple(p) if p else None
            node.ctx_fill = nd.get("ctx_fill")
            bus.nodes[node.id] = node
            bus.order.append(node.id)
        return bus

    def resume(self) -> None:
        """継続実行の開始: reset()と違いツリー・成果物は保持したまま実行中フラグだけ立てる。"""
        self.running = True
        self._publish({"type": "resumed", "run_started_at": self.run_started_at})

    # ---- オーケストレーターから呼ぶAPI ----
    def reset(self) -> None:
        self.nodes.clear()
        self.order.clear()
        self.artifacts.clear()
        self.approvals.clear()
        self.run_started_at = time.time()
        self.running = True
        self._publish({"type": "reset", "run_started_at": self.run_started_at})

    def create_node(self, kind: str, title: str, detail: str = "", parent_id: str | None = None) -> str:
        node_id = uuid.uuid4().hex[:8]
        node = Node(node_id, parent_id, kind, title, detail)
        self.nodes[node_id] = node
        self.order.append(node_id)
        self._publish({"type": "node_created", "node": node.snapshot()})
        return node_id

    def set_status(self, node_id: str, status: str) -> None:
        node = self.nodes[node_id]
        node.status = status
        if status in ("thinking", "generating", "running") and node.started_at is None:
            node.started_at = time.time()
        if status in ("done", "error"):
            node.finished_at = time.time()
        self._publish({"type": "status_changed", "id": node_id, "status": status,
                       "started_at": node.started_at, "finished_at": node.finished_at})

    def set_prompt(self, node_id: str, prompt: str) -> None:
        self.nodes[node_id].prompt = prompt
        self._publish({"type": "prompt", "id": node_id, "prompt": prompt})

    def set_title(self, node_id: str, title: str) -> None:
        self.nodes[node_id].title = title
        self._publish({"type": "title", "id": node_id, "title": title})

    def token_progress(self, node_id: str, piece: str) -> None:
        node = self.nodes[node_id]
        node.tokens += 1
        if len(node.output) < OUTPUT_CAP:
            node.output += piece
        node.preview = node.output[-120:]
        if node.status != "generating":
            # set_status 経由で正規の status_changed を発行(started_at も立つ)
            self.set_status(node_id, "generating")
        self._tok_dirty.add(node_id)
        self._schedule_flush()

    def think_progress(self, node_id: str, piece: str) -> None:
        """thinking(推論)テキストのストリーム。出力本文とは別枠で保持・配信する。

        思考トークンもGPUが生成している負荷そのものなので、状態とトークン計数は
        content と同じ扱いにする(ツール呼び出し中心の応答では content が空のまま
        思考だけが流れるため、これを数えないと t/s も停滞検知も働かない)。
        実測値は区切りごとに set_tokens で上書きされるため、近似カウントでよい。
        """
        node = self.nodes[node_id]
        node.think += piece
        if len(node.think) > THINK_CAP:
            node.think = node.think[-THINK_CAP:]
        node.tokens += 1
        if node.status != "generating":
            self.set_status(node_id, "generating")
        self._tok_dirty.add(node_id)
        self._think_buf.setdefault(node_id, []).append(piece)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_stream()   # ループ外(テスト等)は即時
            return
        self._flush_handle = loop.call_later(FLUSH_INTERVAL, self._flush_stream)

    def _flush_stream(self) -> None:
        self._flush_handle = None
        for node_id in self._tok_dirty:
            node = self.nodes.get(node_id)
            if node is not None and node.status not in ("done", "error", "cancelled"):
                self._publish({"type": "token_progress", "id": node_id,
                               "tokens": node.tokens, "preview": node.preview})
        self._tok_dirty.clear()
        for node_id, pieces in self._think_buf.items():
            node = self.nodes.get(node_id)
            if node is not None:
                # total を添える: snapshot直後に接続したクライアントが、snapshotに
                # 含まれ済みのバッファ分を二重追記しないための冪等キー
                self._publish({"type": "think_progress", "id": node_id,
                               "piece": "".join(pieces), "total": len(node.think)})
        self._think_buf.clear()

    def add_tokens(self, node_id: str, n: int, ctx_fill: float | None = None) -> None:
        """非ストリーミング呼び出し(coderノード等)のトークン計上(加算)。

        ctx_fill: コンテキスト充填率(実測prompt tokens / num_ctx)。UIの窓メーター用。
        """
        node = self.nodes[node_id]
        node.tokens += n
        if ctx_fill is not None:
            node.ctx_fill = ctx_fill
        self._publish({"type": "tokens", "id": node_id, "tokens": node.tokens,
                       "ctx_fill": getattr(node, "ctx_fill", None)})

    def set_tokens(self, node_id: str, total: int, ctx_fill: float | None = None) -> None:
        """トークン数を実測値で確定する(ストリーミングノードの区切り時用)。

        token_progress はチャンク数≒トークンの近似カウントなので、done チャンクの
        eval_count(thinking含む実測)で置き換える。加算だと二重計上になる。
        コーディングエージェントは1ノードで複数回LLMを呼ぶため、呼び出し側が
        累計値を渡す。
        """
        node = self.nodes[node_id]
        node.tokens = total
        if ctx_fill is not None:
            node.ctx_fill = ctx_fill
        self._publish({"type": "tokens", "id": node_id, "tokens": node.tokens,
                       "ctx_fill": getattr(node, "ctx_fill", None)})

    def emit_log(self, node_id: str, line: str) -> None:
        """coder ノードの逐次ログ1行。"""
        node = self.nodes[node_id]
        node.log.append(line)
        if len(node.log) > LOG_CAP:
            del node.log[: len(node.log) - LOG_CAP]
        self._publish({"type": "log_line", "id": node_id, "line": line})

    def set_progress(self, node_id: str, done: int, total: int) -> None:
        self.nodes[node_id].progress = (done, total)
        self._publish({"type": "progress", "id": node_id, "done": done, "total": total})

    def complete(self, node_id: str, output: str | None = None, error: str | None = None) -> None:
        node = self.nodes[node_id]
        if output is not None:
            node.output = output
            node.preview = output[-120:]
        if error is not None:
            node.output = error
            node.preview = error[:120]
        self.set_status(node_id, "error" if error else "done")
        self._publish({"type": "node_completed", "id": node_id,
                       "output": node.output, "error": error})

    # ---- 承認フロー ----
    def request_approval(self, aid: str, node_id: str, command: str, cwd: str) -> None:
        self.approvals[aid] = {"aid": aid, "node_id": node_id, "command": command,
                               "cwd": cwd, "resolved": False, "approved": None}
        self._publish({"type": "approval_requested", "aid": aid, "node_id": node_id,
                       "command": command, "cwd": cwd})

    def resolve_approval(self, aid: str, approved: bool) -> None:
        a = self.approvals.get(aid)
        if a is not None:
            a["resolved"] = True
            a["approved"] = approved
        self._publish({"type": "approval_resolved", "aid": aid, "approved": approved})

    def cancel_all(self, reason: str) -> None:
        """未完了ノードをすべて中断扱いにし、実行終了として締める。"""
        for node_id in self.order:
            node = self.nodes[node_id]
            if node.status not in ("done", "error"):
                node.status = "cancelled"
                node.finished_at = time.time()
                self._publish({"type": "status_changed", "id": node_id, "status": "cancelled",
                               "started_at": node.started_at, "finished_at": node.finished_at})
        self.run_finished(error=reason)

    def set_artifacts(self, artifacts: list[dict]) -> None:
        self.artifacts = artifacts
        self._publish({"type": "artifacts", "artifacts": artifacts})

    def run_finished(self, error: str | None = None) -> None:
        self.running = False
        self._publish({"type": "run_finished", "error": error})
