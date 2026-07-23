"""Step 2: function-calling ループの心臓部を最小構成で理解する。

run_command 1個(+補助 read/list)を LLM に持たせ、tool_call → 実行 → 結果を戻して継続。
tool_call のパース失敗はリトライする(小型ローカルモデルは崩れやすい前提)。

使い方: python step2_tool.py "projects/hello に日時を出すpythonを書いて実行して"
"""
import sys
import json
from llm import load_config, chat
from tools import TOOLS_SCHEMA, run_tool

SYSTEM = (
    "あなたはツールを使えるアシスタント。必要ならツールを呼び、"
    "結果を見て次の手を決める。完了したら finish を呼ぶ。日本語で簡潔に。"
)
MAX_ITER = 12


def main():
    goal = sys.argv[1] if len(sys.argv) > 1 else "projects/hello に現在時刻を表示するpythonを作り実行して"
    cfg = load_config()
    key = cfg.get("default", "coder")
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": goal}]

    for i in range(1, MAX_ITER + 1):
        print(f"\n=== iter {i} (model={key}) ===")
        resp = _chat_with_retry(cfg, key, messages)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            print(msg.content or "(空応答)")
            break

        done = False
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"  -> {name}({args})")
            if name == "finish":
                print("  [完了]", args.get("summary", ""))
                done = True
                result = "done"
            else:
                result = run_tool(name, args)
                print(f"     {str(result)[:300]}")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": str(result)})
        if done:
            break


def _chat_with_retry(cfg, key, messages, tries=3):
    last = None
    for _ in range(tries):
        try:
            return chat(cfg, key, messages, tools=TOOLS_SCHEMA)
        except Exception as e:
            last = e
            print(f"  [retry] {e}")
    raise last


if __name__ == "__main__":
    main()
