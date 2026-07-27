"""AI Agent Lab デスクトップアプリ。

uvicornサーバーをバックグラウンドスレッドで起動し、
ネイティブウィンドウ(WebView2)でダッシュボードを表示する。
Ollamaが未起動なら自動で起動する。

agent-orchestra の app.py を移植(旧D:ドライブ参照を除去。
OLLAMA_* 環境変数はシステム/ユーザー環境変数に委ね、ここでは上書きしない)。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

import uvicorn
import webview

_LOG = (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent) / "app.log"


def log(msg: str) -> None:
    try:
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


OLLAMA_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _free_port(preferred: int = 8765) -> int:
    if not _port_open(preferred):
        return preferred
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_ollama() -> None:
    if _port_open(11434):
        return
    if not OLLAMA_EXE.exists():
        return  # UI側の「未接続」表示に任せる
    subprocess.Popen(
        [str(OLLAMA_EXE), "serve"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for _ in range(20):
        if _port_open(11434):
            return
        time.sleep(0.5)


def start_server(port: int) -> dict:
    """uvicornをバックグラウンドスレッドで起動し、graceful shutdown用の参照を返す。"""
    holder: dict = {}

    def _run() -> None:
        try:
            from server import app  # 起動を速くするため遅延import

            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            srv = uvicorn.Server(config)
            holder["server"] = srv
            log(f"uvicorn starting on {port}")
            srv.run()
            log("uvicorn exited")
        except Exception:
            log("server thread crashed:\n" + traceback.format_exc())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    holder["thread"] = t
    return holder


def wait_ready(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body { background:#0d1117; color:#8b949e; font-family:'Segoe UI',sans-serif;
       display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }
.box { text-align:center; }
.spin { width:36px; height:36px; border:4px solid #30363d; border-top-color:#58a6ff;
        border-radius:50%; margin:0 auto 16px; animation:s .8s linear infinite; }
@keyframes s { to { transform:rotate(360deg); } }
</style></head><body><div class="box"><div class="spin"></div>起動しています…</div></body></html>"""


def main() -> None:
    log("=== app start ===")
    ensure_ollama()
    log(f"ollama alive: {_port_open(11434)}")
    port = _free_port(8765)
    log(f"port chosen: {port}")
    holder = start_server(port)
    url = f"http://127.0.0.1:{port}"

    window = webview.create_window(
        "🧪 AI Agent Lab", html=LOADING_HTML,
        width=1150, height=850, min_size=(700, 500), background_color="#0d1117",
    )

    shown = threading.Event()
    window.events.shown += shown.set

    def _load_when_ready() -> None:
        try:
            log(f"window shown: {shown.wait(30)}")
            ok = wait_ready(url, timeout=60.0)
            log(f"wait_ready -> {ok}")
            if ok:
                window.load_url(url)
                log("load_url called")
            else:
                window.load_html(
                    "<body style='background:#0d1117;color:#f85149;font-family:sans-serif;"
                    "display:flex;align-items:center;justify-content:center;height:100vh'>"
                    "サーバーの起動に失敗しました。app.log を確認してください。</body>"
                )
        except Exception:
            log("load thread crashed:\n" + traceback.format_exc())

    threading.Thread(target=_load_when_ready, daemon=True).start()
    log("starting webview (edgechromium)")
    webview.start(gui="edgechromium")
    log("webview closed")
    # ウィンドウクローズ時にuvicornを正規シャットダウンし、実行中Runの
    # CancelledError→persist(記録保存)を走らせてから終了する
    srv = holder.get("server")
    if srv is not None:
        srv.should_exit = True
        holder["thread"].join(timeout=10)
        log("uvicorn shutdown complete")


if __name__ == "__main__":
    main()
