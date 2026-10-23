"""分析エージェント: 記録から数えた事実だけをローカル LLM に渡し、平易な解説と改善案を書かせる。

LLM の出力は推定(根拠の分類 D)として画面に出す。事実(数値)はこちらで数えたものだけを使い、
LLM に計算させたり、記録の本文を読ませたりはしない。
"""
from __future__ import annotations

import json
import time

import llm

SYSTEM = """あなたは AI エージェントの実行記録を読み解く分析担当です。
渡される JSON は、記録から機械的に数えた事実です。次の3節で、日本語の平易な文章で答えてください。

## 何が起きたか
3〜5文。専門用語には短い言い換えを添える。

## 気になる点
箇条書き3つまで。必ず JSON の数値を引用する。

## 改善案
箇条書き3つまで。効果が大きそうな順。トークン・時間・品質のどれに効くかを書く。

守ること: JSON に無い数値や事実を作らない。分からないことは「記録からは分からない」と書く。"""


async def explain(facts: dict, model_key: str = "worker") -> dict:
    cfg = llm.load_config()
    t0 = time.time()
    msg = await llm.chat(cfg, model_key, [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(facts, ensure_ascii=False, indent=1)},
    ], num_predict=1500, timeout=300)  # think はモデル既定に任せる(gpt-oss は false を受け付けない)
    return {"text": (msg.get("content") or "").strip(), "model": llm.resolve(cfg, model_key)["tag"],
            "seconds": round(time.time() - t0, 1), "facts": facts, "evidence_class": "D"}
