"""共有モジュール: models.yaml の読込 と Ollama(OpenAI互換)クライアント。

全 Step で使い回す最小の土台。tier(local / cloud)で接続先を切替える。
"""
import os
import sys
import yaml
from openai import OpenAI

# Windows コンソール(cp932)で日本語/絵文字が UnicodeEncodeError になるのを防ぐ
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "models.yaml")
LOCAL_BASE = "http://localhost:11434/v1"


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_client(tier="local"):
    """tier に応じた OpenAI 互換クライアントを返す。

    local = ローカル Ollama(localhost)。api_key は形式的でよい。
    cloud = Ollama Cloud 等。OLLAMA_API_KEY と OLLAMA_CLOUD_BASE を使う(外部送信)。
    """
    if tier == "cloud":
        base = os.environ.get("OLLAMA_CLOUD_BASE", "https://ollama.com/v1")
        key = os.environ.get("OLLAMA_API_KEY", "")
        if not key:
            raise RuntimeError("cloud tier には OLLAMA_API_KEY が必要(外部送信・課金)")
        return OpenAI(base_url=base, api_key=key)
    return OpenAI(base_url=LOCAL_BASE, api_key="ollama")


def resolve(cfg, key):
    """モデルキー(coder等)→ (タグ, tier)。未知キーはそのままタグ扱い。"""
    m = cfg.get("models", {}).get(key)
    if m is None:
        return key, "local"
    return m["tag"], m.get("tier", "local")


def chat(cfg, key, messages, tools=None, temperature=0.2):
    """1回の chat.completions 呼び出し。

    文脈長(num_ctx)はモデル側(派生モデル)に焼き込み済み。OpenAI 互換
    エンドポイントは extra_body の num_ctx を無視するため、ここでは送らない。
    """
    tag, tier = resolve(cfg, key)
    client = make_client(tier)
    kwargs = dict(model=tag, messages=messages, temperature=temperature)
    if tools:
        kwargs["tools"] = tools
    return client.chat.completions.create(**kwargs)
