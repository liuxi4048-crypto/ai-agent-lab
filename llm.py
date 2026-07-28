"""共有モジュール: models.yaml の読込 と Ollama ネイティブ /api/chat クライアント。

/v1(OpenAI互換)ではなくネイティブAPIを使う理由:
- options.num_ctx がリクエスト単位で有効(0.32.1で実測確認済み)。
  → 従来の「派生モデルに num_ctx を焼き込む」ワークアラウンドは不要になった。
- tools(function calling)・streaming・format=json も全て対応。
注意: ネイティブAPIの tool_calls は arguments が dict(OpenAI互換はJSON文字列)、
tool_call_id は存在しない。ツール結果は role=tool を呼び出し順で対応付ける。
"""
import asyncio
import contextlib
import json
import os
import sys

import httpx
import yaml

# Windows コンソール(cp932)で日本語/絵文字が UnicodeEncodeError になるのを防ぐ
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "models.yaml")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
DEFAULT_NUM_CTX = 16384
RETRIES = 3
TIMEOUT = httpx.Timeout(900.0, connect=10.0)


class OllamaError(Exception):
    pass


# hybrid(VRAM+RAMオフロード)モデルは、Run内部の並列呼び出し(orchestra/swarmの
# asyncio.gather)もここで直列化する。runs.py の hybrid_lock はRun単位の直列化・
# モデル再ロードスラッシング防止が役割で、両方必要。
_HYBRID_GATE = asyncio.Lock()


def _gate(info):
    return _HYBRID_GATE if info["placement"] == "hybrid" else contextlib.nullcontext()


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(cfg, key):
    """モデルキー(coder等)→ モデル情報 dict。未知キーはタグ直指定として扱う。

    返り値: {key, tag, family, placement, tools, num_ctx, strengths, use}
    """
    m = cfg.get("models", {}).get(key)
    if m is None:
        return {"key": key, "tag": key, "family": "unknown", "placement": "vram",
                "tools": True, "num_ctx": DEFAULT_NUM_CTX, "keep_alive": "30m",
                "num_gpu": None, "ram_gb": 0, "strengths": [], "for": "", "use": ""}
    return {
        "key": key,
        "tag": m["tag"],
        "family": m.get("family", "unknown"),
        "placement": m.get("placement", "vram"),
        "tools": m.get("tools", False),
        "num_ctx": m.get("num_ctx", DEFAULT_NUM_CTX),
        "keep_alive": m.get("keep_alive", "30m"),
        "num_gpu": m.get("num_gpu"),   # None=Ollama自動 / 0=CPU専用 / n=GPUへ載せる層数
        "ram_gb": m.get("ram_gb", 0),  # RAM側に必要な概算容量
        "strengths": m.get("strengths", []),
        "for": m.get("for", ""),       # UI表示用の短い用途
        "use": m.get("use", ""),
    }


def model_catalog(cfg):
    """UI用: 全モデルの情報リスト(既定キー含む)。"""
    return {
        "models": [resolve(cfg, k) for k in cfg.get("models", {})],
        "default": cfg.get("default", ""),
    }


def _payload(info, messages, tools, temperature, num_ctx, json_mode, stream):
    payload = {
        "model": info["tag"],
        "messages": messages,
        "stream": stream,
        # 既定5分のアンロードを防ぐ(承認待ち・長いrun_command後の再ロード25〜65秒対策)。
        # 大型hybridはmodels.yaml側で短め(10m)を指定してRAM占有を抑える。
        "keep_alive": info["keep_alive"],
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx or info["num_ctx"],
        },
    }
    # 層のGPU/CPU配分を明示指定する場合のみ送る(未指定はOllamaの自動判断に任せる)
    if info.get("num_gpu") is not None:
        payload["options"]["num_gpu"] = info["num_gpu"]
    if tools and info["tools"]:
        payload["tools"] = tools
    if json_mode:
        payload["format"] = "json"
    return payload


async def chat(cfg, key, messages, tools=None, temperature=0.2,
               num_ctx=None, json_mode=False):
    """1回の非ストリーミング呼び出し。message dict(content / tool_calls)を返す。

    ツールループは必ずこちらを使う(tools+stream の併用はバージョン依存が残るため)。
    """
    info = resolve(cfg, key)
    payload = _payload(info, messages, tools, temperature, num_ctx, json_mode, False)
    last_err = None
    async with _gate(info):
        for attempt in range(RETRIES):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    resp = await client.post(f"{OLLAMA_BASE}/api/chat", json=payload)
                    if resp.status_code != 200:
                        raise OllamaError(f"Ollama HTTP {resp.status_code}: {resp.text[:500]}")
                    data = resp.json()
                    if data.get("error"):
                        raise OllamaError(data["error"])
                    msg = data.get("message", {})
                    # 生成トークン数を観測用に添付(呼び出し側は履歴追加前に pop する)
                    if data.get("eval_count"):
                        msg["_usage"] = data["eval_count"]
                    return msg
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_err = OllamaError(
                    f"Ollamaに接続/応答できません ({OLLAMA_BASE})。ollama serve の起動を確認: {e}")
            except OllamaError as e:
                last_err = e
            if attempt < RETRIES - 1:
                await asyncio.sleep(2 * (attempt + 1))
    raise last_err


async def chat_stream(cfg, key, messages, temperature=0.7,
                      num_ctx=None, json_mode=False):
    """ストリーミング呼び出し。生成トークン片を逐次yieldする(ツールなし用途)。"""
    info = resolve(cfg, key)
    payload = _payload(info, messages, None, temperature, num_ctx, json_mode, True)
    async with _gate(info):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                async with client.stream("POST", f"{OLLAMA_BASE}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        raise OllamaError(f"Ollama HTTP {resp.status_code}: {body[:500]}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise OllamaError(chunk["error"])
                        piece = chunk.get("message", {}).get("content", "")
                        if piece:
                            yield piece
                        if chunk.get("done"):
                            return
        except httpx.ConnectError as e:
            raise OllamaError(
                f"Ollamaに接続できません ({OLLAMA_BASE})。`ollama serve` が起動しているか確認してください。"
            ) from e


def chat_sync(cfg, key, messages, **kwargs):
    """同期ラッパ(step1_chat 等のCLIスクリプト用)。"""
    return asyncio.run(chat(cfg, key, messages, **kwargs))


async def list_models():
    """インストール済みOllamaモデル名の一覧。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
    except httpx.HTTPError:
        return []


async def is_alive():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


# ダッシュボードのVRAMメーター用。物理VRAM総量はOllama APIから取れないため環境変数で設定
VRAM_TOTAL_BYTES = int(float(os.environ.get("VRAM_TOTAL_GB", "16")) * 1024**3)


def free_ram_gb():
    """空きシステムRAM(GB)。取得できなければ None。

    RAM併用の大型モデルを選ぶ前に、ページングで極端に遅くならないかを判断するのに使う。
    """
    try:
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _Status()
        st.dwLength = ctypes.sizeof(_Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return st.ullAvailPhys / (1024 ** 3)
    except Exception:
        return None


async def gpu_status():
    """Ollama /api/ps からロード中モデルのVRAM使用量を集計する(GET /gpu 用)。"""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/ps")
            resp.raise_for_status()
            models = resp.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return {"available": False, "total_bytes": VRAM_TOTAL_BYTES, "used_bytes": 0, "models": []}
    return {
        "available": True,
        "total_bytes": VRAM_TOTAL_BYTES,
        "used_bytes": sum(m.get("size_vram", 0) for m in models),
        "models": [{"name": m.get("name", ""), "size_vram": m.get("size_vram", 0)} for m in models],
    }
