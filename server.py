"""server.py — 自作コーディングエージェントの Web UI(Python標準ライブラリのみ / 追加依存なし)。

ローカルHTTPサーバ + Server-Sent Events(SSE)で、モダンで見やすいダッシュボードを提供する。
・使用モデル(key + 実タグ)を明示。
・AIの動き(フェーズ PLAN/BUILD/RUN/FIX・思考発話・ツール実行)をライブ表示。
・タスクを並列実行(Claude Code 風)。各タスクはカード + 専用ログ。
・「実行前に承認」ONなら run_command ごとに承認モーダル。

起動:  python server.py   →  http://127.0.0.1:8765 をブラウザで開く
バックエンドは agent.run_agent を再利用(ロジックは agent.py 側)。
"""
import os
import json
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llm import load_config, resolve
import tools
import agent

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "web", "index.html")
HOST, PORT = "127.0.0.1", 8765
LOG_CAP = 3000  # タスクごとのログ保持上限(行)

cfg = load_config()

# ---- イベントバス(SSE 購読者へブロードキャスト) --------------------------
_subs = set()
_subs_lock = threading.Lock()


def publish(ev):
    dead = []
    with _subs_lock:
        for q in _subs:
            try:
                q.put_nowait(ev)
            except Exception:
                dead.append(q)
        for q in dead:
            _subs.discard(q)


def subscribe():
    q = queue.Queue(maxsize=10000)
    with _subs_lock:
        _subs.add(q)
    return q


def unsubscribe(q):
    with _subs_lock:
        _subs.discard(q)


# ---- タスク管理 ------------------------------------------------------------
tasks = {}          # id -> task dict
tasks_lock = threading.Lock()
approvals = {}      # approval id -> {ev, box, task}
_counter = {"task": 0, "appr": 0}
_counter_lock = threading.Lock()


def _next(kind):
    with _counter_lock:
        _counter[kind] += 1
        return _counter[kind]


def status_dict(t):
    return {
        "type": "status", "task": t["id"], "title": t["title"],
        "model": t["model"], "tag": t["tag"], "approve": t["approve"],
        "phase": t["phase"], "iter": t["iter"], "max": t["max_iter"],
        "state": t["state"],
    }


def _emit(t, line):
    line = str(line)
    buf = t["log"]
    buf.append(line)
    if len(buf) > LOG_CAP:
        del buf[: len(buf) - LOG_CAP]
    publish({"type": "log", "task": t["id"], "line": line})


def _on_status(t, d):
    typ = d.get("type")
    if typ == "start":
        t["tag"] = d.get("tag", "")
    elif typ == "iter":
        t["phase"] = d.get("phase", t["phase"])
        t["iter"] = d.get("iter", 0)
        t["state"] = "running"
    elif typ == "end":
        t["state"] = d.get("reason", "done")
    publish(status_dict(t))


def _run(t):
    try:
        agent.run_agent(
            t["goal"], model=t["model"], max_iter=t["max_iter"], approve=t["approve"],
            emit=lambda s: _emit(t, s), should_stop=t["stop"].is_set,
            on_status=lambda d: _on_status(t, d),
        )
    except Exception as e:
        _emit(t, f"\n[error] {e}")
        t["state"] = "error"
    finally:
        if t["state"] == "running":
            t["state"] = "done"
        _emit(t, "\n=== 実行終了 ===")
        publish(status_dict(t))


def create_task(goal, model, max_iter, approve):
    tid = _next("task")
    tag, _ = resolve(cfg, model)
    short = goal.strip().replace("\n", " ")
    short = (short[:40] + "…") if len(short) > 40 else short
    t = {
        "id": tid, "title": short, "goal": goal, "model": model, "tag": tag,
        "approve": approve, "max_iter": max_iter, "phase": "PLAN", "iter": 0,
        "state": "running", "log": [], "stop": threading.Event(), "thread": None,
    }
    with tasks_lock:
        tasks[tid] = t
    publish(status_dict(t))
    th = threading.Thread(target=_run, args=(t,), name=f"task-{tid}", daemon=True)
    t["thread"] = th
    th.start()
    return tid


def approver(command, cwd):
    """tools.APPROVER。worker スレッドから呼ばれる。thread 名で帰属タスク判定。"""
    name = threading.current_thread().name
    tid = int(name.split("-")[1]) if name.startswith("task-") else None
    aid = _next("appr")
    ev = threading.Event()
    box = {}
    approvals[aid] = {"ev": ev, "box": box, "task": tid}
    publish({"type": "approval", "id": aid, "task": tid,
             "command": command, "cwd": cwd})
    ev.wait()
    approvals.pop(aid, None)
    return box.get("ok", False)


tools.APPROVER = approver


def resolve_approval(aid, ok):
    a = approvals.get(aid)
    if a:
        a["box"]["ok"] = bool(ok)
        a["ev"].set()
        publish({"type": "approval_done", "id": aid})


def stop_task(tid):
    t = tasks.get(tid)
    if not t:
        return
    t["stop"].set()
    for aid, a in list(approvals.items()):
        if a["task"] == tid:
            resolve_approval(aid, False)


def snapshot():
    with tasks_lock:
        return [{
            "id": t["id"], "title": t["title"], "model": t["model"], "tag": t["tag"],
            "approve": t["approve"], "phase": t["phase"], "iter": t["iter"],
            "max": t["max_iter"], "state": t["state"], "log": list(t["log"]),
        } for t in tasks.values()]


def models_list():
    out = []
    for k, m in cfg.get("models", {}).items():
        if m.get("tools", True):
            out.append({"key": k, "tag": m["tag"], "use": m.get("use", "")})
    return {"models": out, "default": cfg.get("default", "coder")}


# ---- HTTP ハンドラ ---------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 標準の逐一ログを抑制

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            try:
                with open(INDEX, "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                self.send_error(404, "index.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/models":
            self._json(models_list())
        elif path == "/api/tasks":
            self._json({"tasks": snapshot()})
        elif path == "/api/events":
            self._sse()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/tasks":
            b = self._body()
            goal = (b.get("goal") or "").strip()
            if not goal:
                return self._json({"error": "goal required"}, 400)
            model = b.get("model") or cfg.get("default", "coder")
            try:
                max_iter = int(b.get("max_iter", 18))
            except (TypeError, ValueError):
                max_iter = 18
            approve = bool(b.get("approve", True))
            tid = create_task(goal, model, max_iter, approve)
            self._json({"id": tid})
        elif path.startswith("/api/tasks/") and path.endswith("/stop"):
            try:
                tid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": "bad id"}, 400)
            stop_task(tid)
            self._json({"ok": True})
        elif path.startswith("/api/approvals/"):
            try:
                aid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": "bad id"}, 400)
            resolve_approval(aid, self._body().get("ok", False))
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _sse(self):
        q = subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # 初回スナップショット
            self._send_event({"type": "snapshot", "tasks": snapshot()})
            while True:
                try:
                    ev = q.get(timeout=15)
                    self._send_event(ev)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            unsubscribe(q)

    def _send_event(self, ev):
        data = json.dumps(ev, ensure_ascii=False)
        self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
        self.wfile.flush()


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[server] http://{HOST}:{PORT}  (Ctrl+C で停止)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
