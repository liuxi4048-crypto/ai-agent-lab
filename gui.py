"""gui.py — 自作コーディングエージェントのデスクトップGUI(tkinter / 追加依存なし)。

・使用モデル名(キー + 実タグ)を明示表示。
・AIの動き(フェーズ PLAN/BUILD/RUN/FIX、思考/発話、ツール実行)をライブ表示。
・Claude Code のように「タスクを並列実行」。各タスクはタブになり、独立ログ・状態・停止を持つ。

起動: python gui.py

並列の注意: ローカル Ollama を共有する。同じモデルのタスクはロード済みモデルを共有して同時処理
できるが、異なるモデルを同時に走らせると再ロードで遅くなる(16GB VRAM の制約)。
"""
import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from llm import load_config, resolve
import tools
import agent

BG = "#1e1e2e"
FG = "#cdd6f4"
ACCENT = "#89b4fa"
PANEL = "#313244"
DARK = "#11111b"

STATE_ICON = {"running": "⏳", "done": "✅", "stopped": "⏹",
              "maxiter": "⚠", "error": "❌", "queued": "…"}
PHASE_COLOR = {"PLAN": "#f9e2af", "BUILD": "#89b4fa",
               "RUN": "#a6e3a1", "FIX": "#f38ba8"}


class Task:
    """1回のエージェント実行(= 1タブ)。"""
    def __init__(self, tid, title, model_key, approve, max_iter):
        self.id = tid
        self.title = title
        self.model_key = model_key
        self.approve = approve
        self.max_iter = max_iter
        self.log_q = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        # 状態(worker が書き、UI が読む。単純な属性代入で thread-safe)
        self.tag = ""
        self.phase = "PLAN"
        self.iter = (0, max_iter)
        self.state = "queued"
        # widgets(UI スレッドで設定)
        self.frame = None
        self.header = None
        self.log = None
        self.stop_btn = None

    def emit(self, s):
        self.log_q.put(str(s))

    def on_status(self, d):
        t = d.get("type")
        if t == "start":
            self.tag = d.get("tag", "")
        elif t == "iter":
            self.phase = d.get("phase", self.phase)
            self.iter = (d.get("iter", 0), d.get("max", self.max_iter))
            self.state = "running"
        elif t == "end":
            self.state = d.get("reason", "done")


class AgentGUI:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.tasks = []
        self.by_threadname = {}          # thread名 -> Task(承認の帰属判定用)
        self.approve_q = queue.Queue()   # (task, command, cwd, event, box)
        self._tid = 0

        root.title("ai-agent-lab — 並列コーディングエージェント")
        root.geometry("1000x760")
        root.configure(bg=BG)

        # モデル選択肢: tool対応のみ。表示は "key (tag)"。
        self.model_items = []  # (display, key)
        for k, m in self.cfg.get("models", {}).items():
            if m.get("tools", True):
                self.model_items.append((f"{k}  ({m['tag']})", k))
        self._build_widgets()

        tools.APPROVER = self._approver  # 承認は GUI ダイアログ経由(全タスク共通)
        self.root.after(100, self._drain)

    # ---- UI 構築 -----------------------------------------------------------
    def _build_widgets(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=12, pady=(12, 6))

        tk.Label(top, text="新しいタスク(作りたいもの)", bg=BG, fg=ACCENT,
                 font=("", 10, "bold")).pack(anchor="w")
        self.goal_text = tk.Text(top, height=3, wrap="word", bg=PANEL, fg=FG,
                                 insertbackground=FG, relief="flat")
        self.goal_text.pack(fill="x", pady=(2, 6))
        self.goal_text.insert("1.0",
                              "projects/notepad-gui に、tkinterで動く簡易メモ帳GUIを作って。"
                              "検証は python -m py_compile のみ(GUIは起動しない)。")

        ctrl = tk.Frame(top, bg=BG)
        ctrl.pack(fill="x")

        tk.Label(ctrl, text="モデル", bg=BG, fg=FG).pack(side="left")
        displays = [d for d, _ in self.model_items]
        default_key = self.cfg.get("default", "coder")
        default_disp = next((d for d, k in self.model_items if k == default_key),
                            displays[0] if displays else "")
        self.model_var = tk.StringVar(value=default_disp)
        ttk.Combobox(ctrl, textvariable=self.model_var, values=displays,
                     width=22, state="readonly").pack(side="left", padx=(4, 12))

        tk.Label(ctrl, text="最大反復", bg=BG, fg=FG).pack(side="left")
        self.iter_var = tk.StringVar(value="18")
        tk.Spinbox(ctrl, from_=1, to=60, textvariable=self.iter_var, width=5,
                   bg=PANEL, fg=FG, relief="flat").pack(side="left", padx=(4, 12))

        self.approve_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="実行前に承認する", variable=self.approve_var,
                       bg=BG, fg=FG, selectcolor=PANEL,
                       activebackground=BG, activeforeground=FG).pack(side="left")

        tk.Button(ctrl, text="＋ タスク追加して実行", command=self.on_add_task,
                  bg=ACCENT, fg="#11111b", relief="flat",
                  font=("", 10, "bold"), padx=16).pack(side="right")

        tk.Label(self.root,
                 text="並列実行: 同じモデルのタスクは同時に走らせても軽い。"
                      "違うモデルの同時実行は再ロードで遅くなる(16GB制約)。",
                 bg=BG, fg="#a6adc8", anchor="w").pack(fill="x", padx=12)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(6, 6))

        self.status = tk.Label(self.root, text="準備完了 / 実行中タスク: 0",
                               bg=BG, fg="#a6adc8", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 10))

    # ---- タスク追加 --------------------------------------------------------
    def on_add_task(self):
        goal = self.goal_text.get("1.0", "end").strip()
        if not goal:
            messagebox.showwarning("入力なし", "目標を入力してください")
            return
        key = next((k for d, k in self.model_items if d == self.model_var.get()),
                   self.cfg.get("default", "coder"))
        try:
            max_iter = int(self.iter_var.get())
        except ValueError:
            max_iter = 18
        approve = self.approve_var.get()

        self._tid += 1
        short = (goal[:22] + "…") if len(goal) > 22 else goal
        task = Task(self._tid, short, key, approve, max_iter)
        self._build_task_tab(task, goal)
        self.tasks.append(task)

        tname = f"task{task.id}"
        self.by_threadname[tname] = task
        task.thread = threading.Thread(target=self._work, args=(task, goal),
                                       name=tname, daemon=True)
        task.state = "running"
        task.thread.start()
        self.nb.select(task.frame)

    def _build_task_tab(self, task, goal):
        frame = tk.Frame(self.nb, bg=BG)
        task.frame = frame
        self.nb.add(frame, text=f"{STATE_ICON['running']} {task.title}")

        head = tk.Frame(frame, bg=BG)
        head.pack(fill="x", padx=8, pady=(8, 4))
        task.header = tk.Label(head, text="", bg=BG, fg=FG,
                               font=("Consolas", 10, "bold"), anchor="w")
        task.header.pack(side="left")
        task.stop_btn = tk.Button(head, text="■ 停止",
                                  command=lambda t=task: self.on_stop(t),
                                  bg="#f38ba8", fg="#11111b", relief="flat", padx=10)
        task.stop_btn.pack(side="right")

        task.log = scrolledtext.ScrolledText(frame, wrap="word", bg=DARK, fg=FG,
                                             insertbackground=FG, relief="flat",
                                             font=("Consolas", 10))
        task.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        task.log.configure(state="disabled")
        self._refresh_header(task)

    # ---- worker スレッド ---------------------------------------------------
    def _work(self, task, goal):
        try:
            agent.run_agent(goal, model=task.model_key, max_iter=task.max_iter,
                            approve=task.approve, emit=task.emit,
                            should_stop=task.stop_event.is_set,
                            on_status=task.on_status)
        except Exception as e:
            task.emit(f"\n[error] {e}")
            task.state = "error"
        finally:
            task.emit("\n=== 実行終了 ===")

    def _approver(self, command, cwd):
        # worker スレッドから呼ばれる。どのタスクかは thread 名で判定。
        task = self.by_threadname.get(threading.current_thread().name)
        ev = threading.Event()
        box = {}
        self.approve_q.put((task, command, cwd, ev, box))
        ev.wait()
        return box.get("ok", False)

    # ---- UI スレッド: 定期ドレイン -----------------------------------------
    def _drain(self):
        running = 0
        for task in self.tasks:
            # ログ反映
            appended = False
            try:
                while True:
                    line = task.log_q.get_nowait()
                    if not appended:
                        task.log.configure(state="normal")
                        appended = True
                    task.log.insert("end", line + "\n")
            except queue.Empty:
                pass
            if appended:
                task.log.see("end")
                task.log.configure(state="disabled")

            alive = task.thread is not None and task.thread.is_alive()
            if alive:
                running += 1
            else:
                if task.state == "running":
                    task.state = "done"
                task.stop_btn.configure(state="disabled")
            self._refresh_header(task)
            self._refresh_tab(task)

        # 承認要求(UI スレッドでダイアログ)
        try:
            while True:
                task, command, cwd, ev, box = self.approve_q.get_nowait()
                label = task.title if task else "?"
                ok = messagebox.askyesno(
                    "コマンド実行の承認",
                    f"[{label}] 次を実行しますか?\n\n$ {command}\n\n(cwd: {cwd})")
                box["ok"] = ok
                ev.set()
        except queue.Empty:
            pass

        self.status.configure(
            text=f"タスク合計: {len(self.tasks)} / 実行中: {running}")
        self.root.after(100, self._drain)

    def _refresh_header(self, task):
        i, mx = task.iter
        tag = f" ({task.tag})" if task.tag else ""
        color = PHASE_COLOR.get(task.phase, FG)
        task.header.configure(
            text=f"モデル: {task.model_key}{tag}   状態: {task.state}   "
                 f"フェーズ: {task.phase}   反復: {i}/{mx}   "
                 f"承認: {'ON' if task.approve else 'OFF(自走)'}",
            fg=color if task.state == "running" else FG)

    def _refresh_tab(self, task):
        icon = STATE_ICON.get(task.state, "…")
        try:
            self.nb.tab(task.frame, text=f"{icon} {task.title}")
        except tk.TclError:
            pass

    # ---- 停止 --------------------------------------------------------------
    def on_stop(self, task):
        task.stop_event.set()
        # 承認待ちで固まらないよう、このタスクの保留承認を却下解放
        pending = []
        try:
            while True:
                pending.append(self.approve_q.get_nowait())
        except queue.Empty:
            pass
        for t, cmd, cwd, ev, box in pending:
            if t is task:
                box["ok"] = False
                ev.set()
            else:
                self.approve_q.put((t, cmd, cwd, ev, box))


def main():
    root = tk.Tk()
    AgentGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
