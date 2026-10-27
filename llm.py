"""共有モジュール: models.yaml の読込と Ollama / OpenAI互換 / Anthropic クライアント。

/v1(OpenAI互換)ではなくネイティブAPIを使う理由:
- options.num_ctx がリクエスト単位で有効(0.32.1で実測確認済み)。
  → 従来の「派生モデルに num_ctx を焼き込む」ワークアラウンドは不要になった。
- tools(function calling)・streaming・format(JSONスキーマ)も全て対応。
注意: ネイティブAPIの tool_calls は arguments が dict(OpenAI互換はJSON文字列)。
Ollamaでは tool_call_id は存在せず、ツール結果は role=tool を呼び出し順で対応付ける。
OpenAI互換バックエンドでは送信時にIDを補い、受信した引数をdictへ戻す。

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
import warnings

import httpx
import yaml

# Windows コンソール(cp932)で日本語/絵文字が UnicodeEncodeError になるのを防ぐ
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

COMPLETION_METADATA_VERSION = 1  # _done_reason includes successful stop

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "models.yaml")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
OPENAI_COMPAT_BASE = os.environ.get("OPENAI_COMPAT_BASE", "http://localhost:8080/v1")
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_MAX_TOKENS = 4096  # コンテキスト長とは独立した生成上限
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


# 自動検出モデル(models.yaml未定義の導入済みタグ)に当てる、ファミリ別の公式推奨サンプリング。
# 値は models.yaml の各系統エントリと揃える(min_p は llama.cpp 既定0.1に化けないよう明示)。
FAMILY_OPTIONS = {
    "qwen35":  {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0},
    "qwen":    {"temperature": 0.7, "top_p": 0.8,  "top_k": 20, "min_p": 0, "repeat_penalty": 1.05},
    "gemma4":  {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0},
    "gemma":   {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0},
    "meta":    {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0},
    "gpt-oss": {"temperature": 1.0, "top_p": 1.0,  "top_k": 0,  "min_p": 0},
    "deepseek": {"temperature": 0.6, "top_p": 0.95, "min_p": 0},
    "glm":     {"temperature": 1.0, "top_p": 0.95, "min_p": 0.01, "repeat_penalty": 1.0},
    "granite": {"temperature": 0.2, "top_p": 0.95, "top_k": 20, "min_p": 0},
    "mistral": {"temperature": 0.15, "top_p": 0.95, "min_p": 0},
    "unknown": {"min_p": 0},
}

# 埋め込み等、チャット/エージェントに使わない導入済みモデルは自動検出から除外
_NON_CHAT_MARKERS = ("bge", "embed", "mxbai", "all-minilm", "nomic-embed", "reranker")


def infer_family(tag: str) -> str:
    """Ollamaタグ名からサンプリング系統を推定する(自動検出モデル用)。"""
    t = tag.lower()
    if any(s in t for s in ("qwen3.8", "qwen3.6", "qwen3.5", "qwen35", "ornith")):
        return "qwen35"
    if "qwen" in t:
        return "qwen"
    if "gemma4" in t:
        return "gemma4"
    if "gemma" in t:
        return "gemma"
    if "gpt-oss" in t:
        return "gpt-oss"
    if "deepseek" in t:
        return "deepseek"
    if "glm" in t:
        return "glm"
    if "granite" in t:
        return "granite"
    if any(s in t for s in ("devstral", "mistral", "magistral", "ministral")):
        return "mistral"
    if any(s in t for s in ("muse-glimmer", "glimmer")):
        return "meta"
    return "unknown"


def default_options(family: str) -> dict:
    return dict(FAMILY_OPTIONS.get(family, FAMILY_OPTIONS["unknown"]))


def resolve(cfg, key):
    """モデルキー(coder等)→ モデル情報 dict。未知キーはタグ直指定として扱う。

    返り値: {key, tag, family, placement, tools, num_ctx, options, think, ...}
    """
    m = cfg.get("models", {}).get(key)
    if m is None:
        # models.yaml未定義キー=導入済みタグの直指定(自動検出)。
        # タグからfamilyを推定して公式サンプリングを当て、tier=discovered で
        # 自動ルーティングからは外す(明示指定=--model/GUI選択でのみ使う)。
        fam = infer_family(key)
        return {"key": key, "tag": key, "family": fam, "placement": "vram",
                "tools": True, "num_ctx": DEFAULT_NUM_CTX, "keep_alive": "30m",
                "num_gpu": None, "ram_gb": 0, "backend": "ollama", "base_url": OLLAMA_BASE,
                "server_name": "",
                "options": default_options(fam), "options_no_think": {},
                "think": None, "tier": "discovered",
                "strengths": [], "for": f"{key} (自動検出)",
                "use": "models.yaml未定義=自動検出。公式サンプリング推定・自動ルーティング対象外(明示指定でのみ使用)"}
    return {
        "key": key,
        "backend": m.get("backend", "ollama"),
        "base_url": (m.get("base_url") or os.environ.get("OPENAI_COMPAT_BASE") or "http://localhost:8080/v1")
                    if m.get("backend") == "openai" else (
                        ANTHROPIC_BASE if m.get("backend") == "anthropic" else OLLAMA_BASE),
        "server_name": m.get("server_name", ""),  # UI表示用: 接続先サーバーの名前(任意)
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


def model_catalog(cfg, installed=None):
    """UI用: 設定モデルと、Ollamaからスキャンした実在モデルの一覧。

    models.yaml に未登録でも導入済みならUIから選べるようにする(scanned)。
    scanned は resolve() のフォールバックで family 推定・tier=discovered が付き、
    自動ルーティングには入らない(明示選択でのみ使用)。埋め込み等は除外。
    """
    configured = [resolve(cfg, k) for k in cfg.get("models", {})]
    known_tags = {m["tag"] for m in configured}
    scanned = []
    for tag in sorted(installed or set()):
        base_tag = tag.removesuffix(":latest")
        if tag in known_tags or base_tag in known_tags:
            continue
        if any(mk in tag.lower() for mk in _NON_CHAT_MARKERS):
            continue
        scanned.append({
            **resolve(cfg, tag),
            "scanned": True,
            "for": "PCから検出",
        })
    return {
        # 実際にPCへ導入済みのモデルを先頭に置き、未導入モデルに埋もれないようにする。
        "models": scanned + configured,
        "installed_models": sorted(installed or set()),
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


def _openai_payload(payload):
    """内部の履歴をコピーし、呼び出し順のツール結果へIDを補う。"""
    messages, pending = [], []
    for original in payload["messages"]:
        m = {k: v for k, v in original.items()
             if not k.startswith("_") and k not in ("tool_name", "thinking")}
        if m.get("tool_calls"):
            calls = []
            pending = []
            for i, call in enumerate(m["tool_calls"]):
                cid = call.get("id") or f"call_{len(messages)}_{i}"
                fn = dict(call["function"])
                if not isinstance(fn.get("arguments"), str):
                    fn["arguments"] = json.dumps(fn.get("arguments", {}), ensure_ascii=False)
                calls.append({"id": cid, "type": "function", "function": fn})
                pending.append(cid)
            m["tool_calls"] = calls
        if m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if cid is None and pending:
                cid = pending[0]
            if cid in pending:
                pending.remove(cid)
            if cid is not None:
                m["tool_call_id"] = cid
        messages.append(m)
    # コンテキスト長・GPU配置はサーバー起動時に設定する。
    options = {k: v for k, v in payload["options"].items()
               if k not in ("num_ctx", "num_gpu", "num_predict")}
    if "num_predict" in payload["options"]:
        options["max_tokens"] = payload["options"]["num_predict"]
    result = {**options, "model": payload["model"], "messages": messages,
              "stream": True, "stream_options": {"include_usage": True}}
    if payload.get("tools"):
        result["tools"] = [{"type": "function", "function": dict(t.get("function", t))}
                           for t in payload["tools"]]
    fmt = payload.get("format")
    if isinstance(fmt, dict):
        result["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "response", "schema": fmt}}
    elif fmt == "json":
        result["response_format"] = {"type": "json_object"}
    return result


async def _openai_events(info, payload, timeout):
    """SSEを逐次通知し、最後に内部形式の応答を返す。"""
    base = info["base_url"].rstrip("/")
    msg = {"role": "assistant", "content": ""}
    thinking, calls, usage = [], {}, {}
    started, first = time.monotonic(), None
    reason = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{base}/chat/completions",
                                     json=_openai_payload(payload)) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    error = OllamaFatal if 400 <= resp.status_code < 500 else OllamaError
                    raise error(f"OpenAI互換 ({base}) HTTP {resp.status_code}: {body[:500]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise OllamaError(f"OpenAI互換 ({base}): {chunk['error']}")
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta") or choice.get("message") or {}
                        for source, target in (("content", "content"), ("reasoning_content", "thinking")):
                            piece = delta.get(source)
                            if piece:
                                if first is None:
                                    first = time.monotonic()
                                if target == "content":
                                    msg["content"] += piece
                                else:
                                    thinking.append(piece)
                                yield {target: piece}
                        for tc in delta.get("tool_calls") or []:
                            call = calls.setdefault(tc.get("index", 0), {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                call["id"] += tc["id"]
                            for field in ("name", "arguments"):
                                call["function"][field] += (tc.get("function") or {}).get(field) or ""
                        if choice.get("finish_reason"):
                            reason = choice["finish_reason"]
    except httpx.ConnectError as e:
        raise OllamaError(f"OpenAI互換に接続できません ({base})。llama-server / Strata が起動しているか確認: {e}") from e
    except (httpx.TransportError, json.JSONDecodeError) as e:
        raise OllamaError(f"OpenAI互換 ({base}) 応答の受信に失敗: {e}") from e
    for call in calls.values():
        try:
            call["function"]["arguments"] = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError as e:
            raise OllamaError(f"OpenAI互換のツール引数が不正: {e}") from e
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    if thinking:
        msg["_thinking"] = "".join(thinking)
    if reason:
        msg["_done_reason"] = reason
    now = time.monotonic()
    msg["_timing"] = {
        "ttft_ms": round((first - started) * 1000, 1) if first is not None else None,
        "wall_ms": round((now - started) * 1000, 1),
        "load_ms": None, "prompt_eval_ms": None, "eval_ms": None,
        "total_ms": None, "tok_per_s": None,
        "prompt_tokens": usage.get("prompt_tokens"), "eval_tokens": usage.get("completion_tokens")}
    for source, target in (("prompt_tokens", "_prompt_tokens"), ("completion_tokens", "_usage")):
        if source in usage:
            msg[target] = usage[source]
    yield {"message": msg}


async def _openai_collect(info, payload, timeout, on_delta):
    async for event in _openai_events(info, payload, timeout):
        if "message" in event:
            return event["message"]
        if on_delta:
            for kind, piece in event.items():
                try:
                    on_delta(kind, piece)
                except Exception:
                    pass  # 表示通知の失敗で生成を止めない


def _anthropic_payload(payload):
    """内部履歴をコンテンツブロックへ変換し、同じロールを結合する。"""
    messages, system = [], []
    # 既存の変換で引数の文字列化とツール結果へのID補完を行う。
    for m in _openai_payload(payload)["messages"]:
        role, content = m["role"], m.get("content") or ""
        if role == "system":
            if content:
                system.append(content)
            continue
        blocks = []
        if role == "tool":
            if not m.get("tool_call_id"):
                raise OllamaFatal("Anthropic: ツール結果に対応する呼び出しIDがありません")
            role = "user"
            blocks.append({"type": "tool_result", "tool_use_id": m["tool_call_id"],
                           "content": content})
        else:
            if content:
                blocks.append({"type": "text", "text": content})
            for call in m.get("tool_calls") or []:
                fn = call["function"]
                blocks.append({"type": "tool_use", "id": call["id"], "name": fn["name"],
                               "input": json.loads(fn["arguments"])})
        if not blocks:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    options = payload["options"]
    result = {"model": payload["model"], "messages": messages,
              "stream": payload.get("stream", True),
              "max_tokens": options.get("num_predict", ANTHROPIC_MAX_TOKENS)}
    if system:
        result["system"] = "\n\n".join(system)
    for key in ("temperature", "top_p", "top_k"):
        if key in options:
            result[key] = options[key]
    if payload.get("tools"):
        result["tools"] = []
        for tool in payload["tools"]:
            fn = tool.get("function", tool)
            result["tools"].append({"name": fn["name"], "description": fn.get("description", ""),
                                    "input_schema": fn.get("parameters", {"type": "object"})})
    if payload.get("format"):
        warnings.warn("Anthropic: json_schema / json_mode は未対応のため通常生成します",
                      UserWarning, stacklevel=2)
    return result


async def _anthropic_events(info, payload, timeout):
    """Messages APIのSSEと非ストリーム応答を同じ内部形式へ正規化する。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not key.strip():
        raise OllamaFatal("ANTHROPIC_API_KEY がありません。環境変数に設定してください "
                          "(bash: export ANTHROPIC_API_KEY=... / PowerShell: $env:ANTHROPIC_API_KEY=...) ")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    msg = {"role": "assistant", "content": ""}
    blocks, fragments, usage = {}, {}, {}
    started, first, reason, complete = time.monotonic(), None, None, False
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{ANTHROPIC_BASE}/messages", headers=headers,
                                     json=_anthropic_payload(payload)) as resp:
                if resp.status_code != 200:
                    # 応答本文に認証情報が含まれていても例外・ログへ出さない。
                    error = OllamaFatal if 400 <= resp.status_code < 500 else OllamaError
                    raise error(f"Anthropic HTTP {resp.status_code}")
                if "application/json" in resp.headers.get("content-type", ""):
                    data = json.loads(await resp.aread())
                    events = [{"type": "message_start", "message": data}]
                    events += [{"type": "content_block_start", "index": i, "content_block": b}
                               for i, b in enumerate(data.get("content", []))]
                    events += [{"type": "message_stop"}]
                    async def source():
                        for event in events:
                            yield event
                else:
                    async def source():
                        # SSEの複数data行もイベント境界で結合する。
                        data = []
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                if data:
                                    yield json.loads("\n".join(data))
                                    data = []
                            elif line.startswith("data:"):
                                data.append(line[5:].lstrip())
                        if data:
                            yield json.loads("\n".join(data))
                async for event in source():
                    typ = event.get("type")
                    piece, target = None, None
                    if typ == "error":
                        raise OllamaError("Anthropic: ストリームエラーを受信しました")
                    if typ == "message_start":
                        data = event.get("message", {})
                        usage.update(data.get("usage") or {})
                        reason = data.get("stop_reason") or reason
                    elif typ == "content_block_start":
                        block = dict(event["content_block"])
                        blocks[event["index"]] = block
                        if block["type"] == "text":
                            piece, target = block.get("text"), "content"
                        elif block["type"] == "thinking":
                            piece, target = block.get("thinking"), "thinking"
                    elif typ == "content_block_delta":
                        delta = event["delta"]
                        if delta["type"] == "input_json_delta":
                            index = event["index"]
                            fragments[index] = fragments.get(index, "") + delta.get("partial_json", "")
                        elif delta["type"] == "text_delta":
                            piece, target = delta.get("text"), "content"
                        elif delta["type"] == "thinking_delta":
                            piece, target = delta.get("thinking"), "thinking"
                    elif typ == "content_block_stop":
                        index = event["index"]
                        if index in fragments:
                            blocks[index]["input"] = json.loads(fragments.pop(index))
                    elif typ == "message_delta":
                        usage.update(event.get("usage") or {})
                        reason = event.get("delta", {}).get("stop_reason") or reason
                    elif typ == "message_stop":
                        complete = True
                        break
                    if piece:
                        if first is None:
                            first = time.monotonic()
                        field = "content" if target == "content" else "_thinking"
                        msg[field] = msg.get(field, "") + piece
                        yield {target: piece}
    except (httpx.TransportError, json.JSONDecodeError) as e:
        raise OllamaError(f"Anthropic 応答の受信に失敗: {type(e).__name__}") from None
    if not complete:
        raise OllamaError("Anthropic: message_stop 前にストリームが切断されました")
    calls = []
    for index, block in sorted(blocks.items()):
        if block["type"] == "tool_use":
            if index in fragments:
                raise OllamaError("Anthropic: ツール引数の受信が完了していません")
            calls.append({"id": block["id"], "type": "function", "function": {
                "name": block["name"], "arguments": block.get("input", {})}})
    if calls:
        msg["tool_calls"] = calls
    msg["_done_reason"] = {"end_turn": "stop", "stop_sequence": "stop",
                           "tool_use": "tool_calls", "max_tokens": "length"}.get(reason, reason or "unknown")
    msg["_timing"] = {
        "ttft_ms": round((first - started) * 1000, 1) if first is not None else None,
        "wall_ms": round((time.monotonic() - started) * 1000, 1),
        "load_ms": None, "prompt_eval_ms": None, "eval_ms": None,
        "total_ms": None, "tok_per_s": None,
        "prompt_tokens": usage.get("input_tokens"), "eval_tokens": usage.get("output_tokens")}
    for source, target in (("input_tokens", "_prompt_tokens"), ("output_tokens", "_usage")):
        if source in usage:
            msg[target] = usage[source]
    yield {"message": msg}


async def _anthropic_collect(info, payload, timeout, on_delta):
    async for event in _anthropic_events(info, payload, timeout):
        if "message" in event:
            return event["message"]
        if on_delta:
            for kind, piece in event.items():
                try:
                    on_delta(kind, piece)
                except Exception:
                    pass  # 表示通知の失敗で生成を止めない


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
                    # Preserve successful completion too: callers must distinguish
                    # a complete response from a stream ending without a done chunk.
                    msg["_done_reason"] = reason or "unknown"
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
                if info.get("backend") == "anthropic":
                    return await _anthropic_collect(info, payload, t, on_delta)
                if info.get("backend") == "openai":
                    return await _openai_collect(info, payload, t, on_delta)
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
    if info.get("backend") in ("openai", "anthropic"):
        events = _anthropic_events if info["backend"] == "anthropic" else _openai_events
        async with _gate(info):
            async for event in events(info, payload, t):
                if "message" not in event:
                    yield event
                    continue
                msg = event["message"]
                meta = {target: msg[source] for source, target in (
                    ("_usage", "eval_count"), ("_prompt_tokens", "prompt_tokens"),
                    ("_done_reason", "done_reason"), ("_timing", "timing")) if source in msg}
                if "_done_reason" in msg:
                    yield {"done": True, "meta": meta}
        return
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


async def probe_openai_models(base_url, timeout=2.5):
    """OpenAI互換サーバーの /models を叩き、公開モデルIDの集合を返す。

    サーバーに届かない・応答が不正な場合は None(到達不可として扱う)。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return None
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0))) as client:
            resp = await client.get(f"{base}/models")
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or []
        return {m["id"] for m in data if isinstance(m, dict) and m.get("id")}
    except Exception:
        return None


def anthropic_ready():
    """Claude API が使える状態か(キーの有無のみ。実呼び出しはしない)。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


async def model_usable(cfg, key, ollama_installed, _probes=None):
    """モデルが今すぐ使えるかを (usable, status) で返す。

    status: ok / not_installed(Ollama未導入) / server_unreachable /
            model_missing(サーバーに tag が無い) / no_api_key
    _probes は base_url → probe_openai_models の結果(None可)のキャッシュ。
    """
    info = resolve(cfg, key)
    backend = info.get("backend", "ollama")
    if backend == "openai":
        base = info["base_url"]
        ids = _probes[base] if _probes is not None and base in _probes \
            else await probe_openai_models(base)
        if ids is None:
            return False, "server_unreachable"
        if ids and info["tag"] not in ids:
            return False, "model_missing"
        return True, "ok"
    if backend == "anthropic":
        return (True, "ok") if anthropic_ready() else (False, "no_api_key")
    tag = info["tag"]
    found = tag in ollama_installed or f"{tag}:latest" in ollama_installed
    return (True, "ok") if found else (False, "not_installed")


async def annotate_models(cfg, models, ollama_installed):
    """カタログ用: 各モデル dict に installed(使えるか) と backend_status を付ける。

    OpenAI互換のプローブは base_url 単位で1回だけ行う。
    """
    bases = sorted({m["base_url"] for m in models
                    if m.get("backend") == "openai" and m.get("base_url")})
    probes = {}
    if bases:
        results = await asyncio.gather(
            *(probe_openai_models(b) for b in bases))
        probes = dict(zip(bases, results))
    for m in models:
        usable, status = await model_usable(cfg, m["key"], ollama_installed, probes)
        m["installed"] = usable
        m["backend_status"] = status
    return models


def unusable_reason(info, status):
    """model_usable の status から、利用者向けの理由文を組む。"""
    tag, base = info["tag"], info.get("base_url", "")
    if status == "server_unreachable":
        return (f"モデル '{tag}' の接続先 ({base}) に接続できません。"
                "llama-server / Strata が起動しているか確認してください")
    if status == "model_missing":
        return (f"接続先 ({base}) にモデル '{tag}' がありません。"
                "サーバー側のモデル名と models.yaml の tag の一致を確認してください")
    if status == "no_api_key":
        return ("Claude API のキー (環境変数 ANTHROPIC_API_KEY) が設定されていません")
    return (f"モデル '{tag}' はOllamaに存在しません。"
            f"`ollama pull {tag}` で導入してください")


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
