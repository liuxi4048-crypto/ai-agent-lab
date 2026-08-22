"""共有モジュール: models.yaml の読込 と Ollama ネイティブ /api/chat クライアント。

/v1(OpenAI互換)ではなくネイティブAPIを使う理由:
- options.num_ctx がリクエスト単位で有効(0.32.1で実測確認済み)。
  → 従来の「派生モデルに num_ctx を焼き込む」ワークアラウンドは不要になった。
- tools(function calling)・streaming・format(JSONスキーマ)も全て対応。
注意: ネイティブAPIの tool_calls は arguments が dict(OpenAI互換はJSON文字列)、
tool_call_id は存在しない。ツール結果は role=tool を呼び出し順で対応付ける。

設計メモ(2026-08-12改修):
- chat() は内部でストリーミング受信して結合する。read timeout がチャンク間隔にのみ
  効くため、低速モデル(deep 1.6tok/s等)の長い生成が「900秒で全体打ち切り」に
  ならない。tools + stream は 0.32系で動作(受信側で tool_calls を蓄積)。
- sampling は models.yaml の options(モデル公式推奨値)を既定とし、呼び出し側の
  明示指定が上書きする。全モデル一律 temperature の旧方式は品質劣化要因だった
  (Qwen公式は貪欲寄りデコードを反復ループの原因として禁止、gpt-ossはtemp1.0推奨)。
- 応答メタは "_" プレフィックスのキーで返す(_usage/_prompt_tokens/_thinking/
  _done_reason/_timing)。履歴に追加する前に strip_meta() で剥がすこと。
- _timing はベンチ(bench.py)用の速度計測: 初トークンまでの実測(ttft_ms)と、
  done チャンクが持つ Ollama 内部の所要時間(load/prompt_eval/eval/total)を ms で返す。
  tok_per_s は eval_count / eval_duration(thinking含む純粋な生成速度)。
"""
import asyncio
import contextlib
import json
import os
import sys
import time

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
CONNECT_TIMEOUT = 10.0
# ストリーミング受信のチャンク間隔に適用される(生成全体の上限ではない)。
# モデルのロード+プロンプト評価が最初のチャンクまでに入るため、大型hybridの
# ロード(〜65秒)+長文prefillを考慮して十分長くする。
IDLE_TIMEOUT = 900.0


class OllamaError(Exception):
    """リトライ対象のエラー(接続断・5xx・途中切断など)。"""


class OllamaFatal(OllamaError):
    """リトライしても無駄なエラー(4xx: モデル不在・不正リクエスト等)。"""


# hybrid(VRAM+RAMオフロード)モデルは、Run内部の並列呼び出し(orchestra/swarmの
# asyncio.gather)もここで直列化する。runs.py の hybrid_lock はRun単位の直列化・
# モデル再ロードスラッシング防止が役割で、両方必要。
# ロックはイベントループごとに生成する(asyncio.Lock はループに束縛されるため)。
# WeakKeyDictionary を使う: id(loop) キーだと閉じたループの id 再利用で
# 「別ループ束縛の Lock」を掴んで RuntimeError になり、エントリも無限成長する。
import weakref

_hybrid_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _gate(info):
    if info["placement"] != "hybrid":
        return contextlib.nullcontext()
    loop = asyncio.get_running_loop()
    lock = _hybrid_locks.get(loop)
    if lock is None:
        lock = _hybrid_locks[loop] = asyncio.Lock()
    return lock


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(cfg, key):
    """モデルキー(coder等)→ モデル情報 dict。未知キーはタグ直指定として扱う。

    返り値: {key, tag, family, placement, tools, num_ctx, options, think, ...}
    """
    m = cfg.get("models", {}).get(key)
    if m is None:
        return {"key": key, "tag": key, "family": "unknown", "placement": "vram",
                "tools": True, "num_ctx": DEFAULT_NUM_CTX, "keep_alive": "30m",
                "num_gpu": None, "ram_gb": 0, "options": {}, "options_no_think": {},
                "think": None, "tier": "agent",
                "strengths": [], "for": "", "use": ""}
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
        "options": m.get("options") or {},  # モデル公式推奨のサンプリング設定
        # thinking を切ったときに使う別プロファイル(Qwen3.8等は thinking/instruct で推奨値が別物)
        "options_no_think": m.get("options_no_think") or {},
        "think": m.get("think"),       # 既定のthinking指定(bool / "low"等)。None=モデル既定
        "tier": m.get("tier", "agent"),  # 選定状態(agent/critic/probation/external/archive)。UI表示用
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


def effort(info, level):
    """モデルファミリーに応じた think パラメータ値を返す(未対応系は None)。

    gpt-oss は "low"/"medium"/"high"、meta(Muse Glimmer)は
    "low"/"medium"/"high"/"xhigh" の文字列、deepseek-r1 は bool。
    未知ファミリーには送らない(Ollamaが think 非対応モデルで 400 を返すため)。
    level: "high" 等。None なら None を返す。
    """
    if level is None:
        return None
    fam = info.get("family", "")
    if fam == "gpt-oss":
        return level if level in ("low", "medium", "high") else "high"
    if fam == "meta":
        return level if level in ("low", "medium", "high", "xhigh") else "high"
    if fam == "qwen35":
        # Qwen3.8系。Ollamaの think は low/medium/high を受理する(実測)。
        # ただし指定レベルと実際の推論量は単調に対応しないため、深さの目安として扱う
        return level if level in ("low", "medium", "high") else "high"
    if fam == "deepseek":
        return level == "high" or None
    return None


def strip_meta(msg):
    """応答メッセージから "_" 始まりのメタキーを取り除き、メタ dict を返す。

    履歴(messages)へ追加する前に必ず呼ぶ。thinking もここで剥がす
    (再送するとコンテキストを浪費し、テンプレートによっては挙動が乱れる)。
    """
    meta = {}
    for k in [k for k in msg if k.startswith("_")]:
        meta[k] = msg.pop(k)
    return meta


def _payload(info, messages, tools, temperature, num_ctx, json_mode, stream,
             json_schema=None, think=None, num_predict=None):
    tk = think if think is not None else info.get("think")
    # thinking を切る場合、サンプリングも non-thinking 用の公式推奨へ入れ替える
    # (Qwen3.8: thinking=temp1.0/top_p0.95 ⇄ instruct=temp0.7/top_p0.8/presence1.5)
    use_no_think = tk is False and info.get("options_no_think")
    options = dict((info.get("options_no_think") if use_no_think else info.get("options")) or {})
    if temperature is not None:
        options["temperature"] = temperature
    options["num_ctx"] = num_ctx or info["num_ctx"]
    if num_predict:
        options["num_predict"] = num_predict
    payload = {
        "model": info["tag"],
        "messages": messages,
        "stream": stream,
        # 既定5分のアンロードを防ぐ(承認待ち・長いrun_command後の再ロード25〜65秒対策)。
        # 大型hybridはmodels.yaml側で短め(10m)を指定してRAM占有を抑える。
        "keep_alive": info["keep_alive"],
        "options": options,
    }
    # 層のGPU/CPU配分を明示指定する場合のみ送る(未指定はOllamaの自動判断に任せる)
    if info.get("num_gpu") is not None:
        payload["options"]["num_gpu"] = info["num_gpu"]
    if tools and info["tools"]:
        payload["tools"] = tools
    if json_schema:
        payload["format"] = json_schema   # 文法制約デコードでスキーマ準拠を強制
    elif json_mode:
        payload["format"] = "json"
    if tk is not None:
        payload["think"] = tk
    return payload


def _classify_http(status, body):
    if 400 <= status < 500:
        return OllamaFatal(f"Ollama HTTP {status}: {body[:500]}")
    return OllamaError(f"Ollama HTTP {status}: {body[:500]}")


def _timing_from_done(chunk, t_start, t_first):
    """done チャンクの所要時間(ns)と実測タイムスタンプから速度メタ(ms単位)を組む。

    ttft_ms はクライアント実測(ロード+プロンプト評価+キュー待ちを含む体感値)。
    load_ms は Ollama 申告のモデルロード時間で、0 に近ければウォーム状態。
    tok_per_s は eval_count/eval_duration(thinking を含む純粋な生成速度)。
    """
    now = time.monotonic()
    ns = lambda k: (chunk.get(k) or 0) / 1e6   # ns → ms
    eval_tokens = chunk.get("eval_count") or 0
    eval_ms = ns("eval_duration")
    return {
        "ttft_ms": round(((t_first or now) - t_start) * 1000, 1),
        "wall_ms": round((now - t_start) * 1000, 1),
        "load_ms": round(ns("load_duration"), 1),
        "prompt_eval_ms": round(ns("prompt_eval_duration"), 1),
        "eval_ms": round(eval_ms, 1),
        "total_ms": round(ns("total_duration"), 1),
        "prompt_tokens": chunk.get("prompt_eval_count") or 0,
        "eval_tokens": eval_tokens,
        "tok_per_s": round(eval_tokens / (eval_ms / 1000), 2) if eval_ms > 0 else None,
    }


async def _stream_collect(payload, timeout, on_delta=None):
    """ストリーミング受信して1つの message dict に結合する。

    content / thinking / tool_calls を蓄積し、done チャンクから
    _usage(eval_count) / _prompt_tokens / _done_reason を添付する。
    on_delta(kind, piece) を渡すと、受信の都度 kind="content"/"thinking" で通知する
    (ツールループ側が生成の進行をUIへ流すために使う。同期・軽量な処理のみ想定)。
    """
    msg = {"role": "assistant", "content": ""}
    thinking: list = []
    tool_calls: list = []
    t_start = time.monotonic()
    t_first = None   # 最初の content/thinking チャンク到着時刻(TTFT)

    def notify(kind, piece):
        if on_delta is None:
            return
        try:
            on_delta(kind, piece)
        except Exception:
            pass   # 表示用の通知が失敗しても生成本体は続行する
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{OLLAMA_BASE}/api/chat", json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise _classify_http(resp.status_code, body)
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise OllamaError(chunk["error"])
                m = chunk.get("message") or {}
                if t_first is None and (m.get("content") or m.get("thinking")):
                    t_first = time.monotonic()
                if m.get("content"):
                    msg["content"] += m["content"]
                    notify("content", m["content"])
                if m.get("thinking"):
                    thinking.append(m["thinking"])
                    notify("thinking", m["thinking"])
                if m.get("tool_calls"):
                    tool_calls.extend(m["tool_calls"])
                if chunk.get("done"):
                    if chunk.get("eval_count"):
                        msg["_usage"] = chunk["eval_count"]
                    if chunk.get("prompt_eval_count"):
                        msg["_prompt_tokens"] = chunk["prompt_eval_count"]
                    reason = chunk.get("done_reason")
                    if reason and reason != "stop":
                        # "length" = num_ctx/num_predict による打ち切り。
                        # 呼び出し側が不完全な生成物と判断できるように残す。
                        msg["_done_reason"] = reason
                    msg["_timing"] = _timing_from_done(chunk, t_start, t_first)
                    break
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if thinking:
        msg["_thinking"] = "".join(thinking)
    return msg


async def chat(cfg, key, messages, tools=None, temperature=None,
               num_ctx=None, json_mode=False, json_schema=None,
               think=None, num_predict=None, timeout=None, retries=RETRIES,
               on_delta=None):
    """1回のLLM呼び出し(内部はストリーミング受信)。message dict を返す。

    temperature=None のときは models.yaml の options(公式推奨値)が使われる。
    json_schema に dict を渡すと Ollama の structured outputs で構造を強制する。
    on_delta(kind, piece) を渡すと生成の進行が逐次通知される(ツールループのUI表示用)。
    返り値には "_usage" 等のメタキーが付く。履歴に足す前に strip_meta() を呼ぶこと。
    """
    info = resolve(cfg, key)
    payload = _payload(info, messages, tools, temperature, num_ctx, json_mode,
                       True, json_schema, think, num_predict)
    t = httpx.Timeout(timeout or IDLE_TIMEOUT, connect=CONNECT_TIMEOUT)
    last_err = None
    async with _gate(info):
        for attempt in range(max(1, retries)):
            try:
                return await _stream_collect(payload, t, on_delta)
            except OllamaFatal:
                raise    # 4xx はリトライしても無駄。即時失敗
            except httpx.ConnectError as e:
                last_err = OllamaError(
                    f"Ollamaに接続できません ({OLLAMA_BASE})。ollama serve の起動を確認: {e}")
            except (httpx.TransportError, json.JSONDecodeError) as e:
                # ReadTimeout / ConnectTimeout / RemoteProtocolError / 途中切断の
                # 不正JSON行 — いずれも一時障害としてリトライ対象
                last_err = OllamaError(f"Ollama応答の受信に失敗: {type(e).__name__}: {e}")
            except OllamaError as e:
                last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
    raise last_err


async def chat_stream(cfg, key, messages, temperature=None,
                      num_ctx=None, json_mode=False, json_schema=None,
                      think=None, num_predict=None):
    """ストリーミング呼び出し。{"content": piece} / {"thinking": piece} /
    {"done": True, "meta": {...}} の dict を逐次yieldする(ツールなし用途)。
    """
    info = resolve(cfg, key)
    payload = _payload(info, messages, None, temperature, num_ctx, json_mode,
                       True, json_schema, think, num_predict)
    t = httpx.Timeout(IDLE_TIMEOUT, connect=CONNECT_TIMEOUT)
    async with _gate(info):
        try:
            async with httpx.AsyncClient(timeout=t) as client:
                async with client.stream("POST", f"{OLLAMA_BASE}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        raise _classify_http(resp.status_code, body)
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise OllamaError(chunk["error"])
                        m = chunk.get("message") or {}
                        if m.get("thinking"):
                            yield {"thinking": m["thinking"]}
                        if m.get("content"):
                            yield {"content": m["content"]}
                        if chunk.get("done"):
                            meta = {}
                            if chunk.get("eval_count"):
                                meta["eval_count"] = chunk["eval_count"]
                            if chunk.get("prompt_eval_count"):
                                meta["prompt_tokens"] = chunk["prompt_eval_count"]
                            reason = chunk.get("done_reason")
                            if reason and reason != "stop":
                                meta["done_reason"] = reason
                            yield {"done": True, "meta": meta}
                            return
        except httpx.ConnectError as e:
            raise OllamaError(
                f"Ollamaに接続できません ({OLLAMA_BASE})。`ollama serve` が起動しているか確認してください。"
            ) from e
        except (httpx.TransportError, json.JSONDecodeError) as e:
            raise OllamaError(f"Ollamaストリームが途中で切断されました: {type(e).__name__}: {e}") from e


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
