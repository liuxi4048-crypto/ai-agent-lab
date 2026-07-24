"""Step 2: function-calling ループの心臓部を最小構成で理解する。

run_command 1個(+補助 read/list)を LLM に持たせ、tool_call → 実行 → 結果を戻して継続。
ネイティブ /api/chat では tool_calls の arguments が dict で届く(JSONパース不要)。

使い方: python step2_tool.py "hello に日時を出すpythonを書いて実行して"
"""
import asyncio
import sys

import llm
from tools import TOOLS_SCHEMA, Toolbox

SYSTEM = (
    "あなたはツールを使えるアシスタント。必要ならツールを呼び、"
    "結果を見て次の手を決める。完了したら finish を呼ぶ。日本語で簡潔に。"
)
MAX_ITER = 12


async def main():
    goal = sys.argv[1] if len(sys.argv) > 1 else "hello に現在時刻を表示するpythonを作り実行して"
    cfg = llm.load_config()
    key = cfg.get("default", "coder")
    tb = Toolbox(approve=True)  # コンソール承認
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": goal}]

    for i in range(1, MAX_ITER + 1):
        print(f"\n=== iter {i} (model={key}) ===")
        msg = await _chat_with_retry(cfg, key, messages)
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            print(msg.get("content") or "(空応答)")
            break

        done = False
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            print(f"  -> {name}({args})")
            if name == "finish":
                print("  [完了]", args.get("summary", ""))
                done = True
                result = "done"
            else:
                result = await tb.run(name, args)
                print(f"     {str(result)[:300]}")
            messages.append({"role": "tool", "tool_name": name, "content": str(result)})
        if done:
            break


async def _chat_with_retry(cfg, key, messages, tries=3):
    last = None
    for _ in range(tries):
        try:
            return await llm.chat(cfg, key, messages, tools=TOOLS_SCHEMA)
        except Exception as e:
            last = e
            print(f"  [retry] {e}")
    raise last


if __name__ == "__main__":
    asyncio.run(main())
