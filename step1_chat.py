"""Step 1: ローカル接続 + 単一モデル運用 + 役割(system prompt)分離の骨格。

使い方: python step1_chat.py
  /models        登録モデル一覧
  /model <key>   モデル切替(例 /model smart)
  /role <plan|code>  system prompt の役割切替
  /quit          終了
"""
import sys
from llm import load_config, chat

ROLE_PROMPTS = {
    "plan": "あなたは設計者。要件を分解し、作るもの・手順・検証方法を簡潔に日本語で計画する。",
    "code": "あなたは実装者。動くコードを最短で書き、簡潔に説明する。",
}


def main():
    cfg = load_config()
    model_key = cfg.get("default", "coder")
    role = "code"
    print(f"[step1] model={model_key} role={role}  (/models /model /role /quit)")

    history = []
    while True:
        try:
            user = input("\nあなた> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        if user == "/quit":
            break
        if user == "/models":
            for k, m in cfg["models"].items():
                print(f"  {k:8} {m['tag']:16} [{m.get('tier','local')}] {m.get('use','')}")
            continue
        if user.startswith("/model "):
            key = user.split(maxsplit=1)[1].strip()
            if key in cfg["models"]:
                model_key = key
                print(f"  -> model={model_key}")
            else:
                print(f"  未知のモデルキー: {key}")
            continue
        if user.startswith("/role "):
            r = user.split(maxsplit=1)[1].strip()
            if r in ROLE_PROMPTS:
                role = r
                print(f"  -> role={role}")
            else:
                print("  role は plan / code")
            continue

        messages = [{"role": "system", "content": ROLE_PROMPTS[role]}] + history + [
            {"role": "user", "content": user}
        ]
        try:
            resp = chat(cfg, model_key, messages)
        except Exception as e:
            print(f"  [error] {e}")
            continue
        answer = resp.choices[0].message.content or ""
        print(f"\n{model_key}> {answer}")
        history += [{"role": "user", "content": user},
                    {"role": "assistant", "content": answer}]


if __name__ == "__main__":
    main()
