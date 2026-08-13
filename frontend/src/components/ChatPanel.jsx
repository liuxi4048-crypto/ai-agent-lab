import { useEffect, useRef, useState } from "react";
import { MessageSquare, Send, Loader2, User, Bot, Play } from "lucide-react";
import { continueRun } from "../api.js";
import Markdown from "./md.jsx";

/**
 * 成果物に会話形式で修正を加えるパネル(ワークスペース下部に常設)。
 *
 * 実行ツリーのノードから会話スレッドを組み立てて表示する:
 *   task ノード          → 最初の依頼(ユーザー)
 *   追加指示の coder     → 追加の依頼(ユーザー)
 *   answer ノード        → その回の結果(エージェント)
 * 送信すると同じRun・同じワークスペースの続きとして実行される。
 */
export default function ChatPanel({ runId, nodes, order, running, resumable, onSent }) {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const endRef = useRef(null);

  const thread = buildThread(nodes, order);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [thread.length, running]);

  if (!runId) return null;

  const send = async () => {
    const text = msg.trim();
    if (!text || busy || running) return;
    setBusy(true);
    setError(null);
    try {
      await continueRun(runId, text);
      setMsg("");
      onSent?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const disabled = busy || running || !resumable;
  const placeholder = running
    ? "実行中です。完了すると追加の指示を送れます…"
    : "成果物への追加指示(例: 難易度を上げて / 色を変えて / スコア表示を追加して)";

  return (
    <section className="border-t border-zinc-800 bg-zinc-950/80">
      <div className="flex items-center gap-2 px-4 pt-3 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
        <MessageSquare size={12} /> 会話で修正する
      </div>

      {thread.length > 0 && (
        <div className="max-h-56 space-y-2 overflow-y-auto px-4 py-2">
          {thread.map((m, i) => (
            <div key={i} className="flex gap-2 text-[12.5px]">
              <span
                className={`mt-0.5 shrink-0 ${m.role === "user" ? "text-blue-400" : "text-green-400"}`}
                title={m.role === "user" ? "あなた" : "エージェント"}
              >
                {m.role === "user" ? <User size={13} /> : <Bot size={13} />}
              </span>
              {m.role === "user" ? (
                <p className="min-w-0 flex-1 whitespace-pre-wrap break-words rounded-lg bg-blue-500/10 px-2.5 py-1.5 text-zinc-200">
                  {m.text}
                </p>
              ) : (
                <Markdown text={m.text} className="min-w-0 flex-1 rounded-lg bg-zinc-900 px-2.5 py-1.5 text-zinc-300" />
              )}
            </div>
          ))}
          <div ref={endRef} />
        </div>
      )}

      <div className="flex items-end gap-2 px-4 pb-3 pt-1">
        <textarea
          value={msg}
          onChange={(e) => setMsg(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={1}
          disabled={disabled}
          placeholder={placeholder}
          className="max-h-32 min-h-[38px] flex-1 resize-none rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-blue-500 disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={disabled || !msg.trim()}
          title="Enterで送信 / Shift+Enterで改行"
          className="flex h-[38px] items-center gap-1.5 rounded-md bg-blue-600 px-4 text-sm font-semibold text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-500"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
          送信
        </button>
      </div>

      {error && <p className="px-4 pb-2 text-xs text-red-400">{error}</p>}
      {!resumable && !running && (
        <p className="flex items-center gap-1.5 px-4 pb-3 text-[11px] text-zinc-600">
          <Play size={10} /> このRunは継続できません(完了後に再度お試しください)
        </p>
      )}
    </section>
  );
}

/** ノード木 → 会話スレッド。追加指示ノードは title の接頭辞で見分ける。 */
function buildThread(nodes, order) {
  const out = [];
  for (const id of order ?? []) {
    const n = nodes?.[id];
    if (!n) continue;
    if (n.kind === "task") {
      out.push({ role: "user", text: n.detail || n.title });
    } else if (n.kind === "coder" && n.title?.startsWith("🛠 追加指示")) {
      out.push({ role: "user", text: n.detail || n.title.replace("🛠 追加指示: ", "") });
    } else if (n.kind === "answer" && n.output) {
      out.push({ role: "agent", text: n.output });
    }
  }
  return out;
}
