"""外部連携の保存: 他のアプリ向けの接続（名前・合言葉・既定モデル）とゲートウェイ設定。

保存先は <repo>/.lab/connections.json（環境変数 LAB_DATA_DIR で変更可、git管理外）。
llm_gateway.py（認証・最終利用の記録）と server.py（ダッシュボードの管理API）が共有する。
接続コード: aal://<host>:<port>?model=<モデル>&key=<合言葉>
"""
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

DEFAULT_PORT = 8766
TOUCH_INTERVAL = 30  # 最終利用の書き込みを同じ接続につき何秒に1回までにするか
_lock = threading.Lock()
_last_touch: dict = {}


def _path() -> Path:
    base = os.environ.get("LAB_DATA_DIR") or Path(__file__).parent / ".lab"
    return Path(base) / "connections.json"


def _default() -> dict:
    return {"gateway": {"host": "", "port": DEFAULT_PORT, "autostart": True}, "connections": []}


def load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    base = _default()
    if isinstance(data, dict):
        if isinstance(data.get("gateway"), dict):
            base["gateway"].update(data["gateway"])
        if isinstance(data.get("connections"), list):
            base["connections"] = [c for c in data["connections"] if isinstance(c, dict)]
    return base


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _find(data: dict, conn_id: str):
    return next((c for c in data["connections"] if c.get("id") == conn_id), None)


def add(name: str, model: str) -> dict:
    conn = {"id": secrets.token_hex(4), "name": name, "token": secrets.token_urlsafe(32),
            "model": model, "created_at": time.time(), "last_used_at": None, "last_client": None}
    with _lock:
        data = load()
        data["connections"].append(conn)
        _save(data)
    return conn


def rotate(conn_id: str):
    """合言葉を作り直す（古いコードは使えなくなる）。無ければ None。"""
    with _lock:
        data = load()
        conn = _find(data, conn_id)
        if conn is None:
            return None
        conn["token"] = secrets.token_urlsafe(32)
        _save(data)
        return conn["token"]


def delete(conn_id: str) -> bool:
    with _lock:
        data = load()
        left = [c for c in data["connections"] if c.get("id") != conn_id]
        if len(left) == len(data["connections"]):
            return False
        data["connections"] = left
        _save(data)
        _last_touch.pop(conn_id, None)
        return True


def match_token(token: str):
    """保存済みの全合言葉と定数時間で比較する。一致した接続か None。"""
    found = None
    for c in load()["connections"]:
        if hmac.compare_digest(token.encode(), str(c.get("token", "")).encode()):
            found = c
    return found


def touch(conn_id: str, client_ip) -> None:
    now = time.time()
    if now - _last_touch.get(conn_id, 0) < TOUCH_INTERVAL:
        return
    _last_touch[conn_id] = now
    with _lock:
        data = load()
        conn = _find(data, conn_id)
        if conn is not None:
            conn["last_used_at"], conn["last_client"] = now, client_ip
            _save(data)


def set_gateway(host=None, port=None, autostart=None) -> dict:
    with _lock:
        data = load()
        gw = data["gateway"]
        if host is not None:
            gw["host"] = host.strip()
        if port is not None:
            if not 1 <= int(port) <= 65535:
                raise ValueError("port は 1〜65535")
            gw["port"] = int(port)
        if autostart is not None:
            gw["autostart"] = bool(autostart)
        _save(data)
        return gw


def default_host() -> str:
    """保存したホスト → このPCの Tailscale IPv4 → 127.0.0.1。"""
    if host := load()["gateway"].get("host"):
        return host
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                             timeout=3).stdout.split()
        if out and out[0].count(".") == 3:
            return out[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return "127.0.0.1"


def connection_code(conn: dict, host: str, port: int) -> str:
    return f"aal://{host}:{port}?model={quote(conn['model'], safe='')}&key={conn['token']}"


def parse_code(code: str) -> dict:
    u = urlsplit(code)
    q = parse_qs(u.query)
    return {"host": u.hostname, "port": u.port, "model": q.get("model", [""])[0],
            "key": q.get("key", [""])[0]}
