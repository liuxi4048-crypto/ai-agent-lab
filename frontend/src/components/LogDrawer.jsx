import { useEffect, useRef, useState } from "react";
import { ArrowDownToLine, Copy, Check } from "lucide-react";
import Drawer from "./Drawer.jsx";
import Markdown from "./md.jsx";
import { AGENT_STATES, KIND_LABEL, ctxFillLevel } from "../derive.js";

const CTX_METER_CLASS = {
  none: "border-zinc-700 text-zinc-500",
  warn: "border-yellow-500 text-yellow-400",
  danger: "border-red-500 text-red-400",
};

function CopyButton({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text || "");
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
      className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
    >
      {done ? <Check size={11} className="text-green-400" /> : <Copy size={11} />}
      {done ? "コピー済み" : "コピー"}
    </button>
  );
}

/**
 * 詳細ログのサイドドロワー。完全なストリーミング出力(CoT含む)と実行ログを表示。
 * 追従スクロールはトグル可能(手動で上へスクロールすると自動解除)。
 */
export default function LogDrawer({ open, onClose, node, agentState }) {
  const [follow, setFollow] = useState(true);
  const [showPrompt, setShowPrompt] = useState(false);
  const scrollRef = useRef(null);

  const outputLen = node?.output?.length ?? 0;
  const logLen = node?.log?.length ?? 0;

  useEffect(() => {
    if (!open || !follow || !scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [open, follow, outputLen, logLen]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (!atBottom && follow) setFollow(false);
  };

  if (!node) return null;
  const meta = agentState ? AGENT_STATES[agentState.key] : null;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      wide
      title={
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-semibold text-zinc-100">{node.title}</span>
          <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
            {KIND_LABEL[node.kind] ?? node.kind}
          </span>
          {meta && (
            <span className={`shrink-0 rounded-full bg-zinc-800/80 px-2 py-0.5 text-[11px] font-semibold ${meta.text}`}>
              {meta.label}
            </span>
          )}
        </div>
      }
    >
      <div className="flex items-center gap-2 border-b border-zinc-800/70 px-4 py-2">
        {node.prompt && (
          <button
            onClick={() => setShowPrompt((v) => !v)}
            className={`rounded-md border px-2 py-1 text-[11px] ${
              showPrompt ? "border-blue-500 text-blue-400" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            プロンプト{showPrompt ? "を隠す" : "を表示"}
          </button>
        )}
        <CopyButton text={node.output} />
        {node.ctx_fill != null && (
          <span
            className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] tabular-nums ${CTX_METER_CLASS[ctxFillLevel(node.ctx_fill)]}`}
            title="コンテキスト充填率(実測prompt tokens / num_ctx)"
          >
            コンテキスト {Math.round(node.ctx_fill * 100)}%
          </span>
        )}
        <button
          onClick={() => {
            setFollow((v) => !v);
            if (!follow && scrollRef.current)
              scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
          }}
          className={`ml-auto flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] ${
            follow ? "border-blue-500 text-blue-400" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
          }`}
        >
          <ArrowDownToLine size={11} /> 追従{follow ? "ON" : "OFF"}
        </button>
      </div>

      <div ref={scrollRef} onScroll={onScroll} className="h-full space-y-4 overflow-y-auto p-4">
        {showPrompt && node.prompt && (
          <section>
            <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">Prompt</h3>
            <pre className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-[12px] leading-relaxed text-zinc-400">
              {node.prompt}
            </pre>
          </section>
        )}
        {node.log?.length > 0 && (
          <section>
            <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
              実行ログ ({node.log.length})
            </h3>
            <pre className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-[12px] leading-relaxed text-zinc-300">
              {node.log.join("\n")}
            </pre>
          </section>
        )}
        {node.think && (
          <section>
            <details className="rounded-lg border border-zinc-800 bg-zinc-900/60">
              <summary className="flex cursor-pointer select-none items-center gap-1.5 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 hover:text-zinc-300">
                🧠 思考
                <span className="tabular-nums font-normal text-zinc-600">({node.think.length.toLocaleString()}字)</span>
              </summary>
              <pre className="whitespace-pre-wrap border-t border-zinc-800 p-3 font-mono text-[12px] leading-relaxed text-zinc-500">
                {node.think}
              </pre>
            </details>
          </section>
        )}
        <section>
          <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            出力 {node.tokens > 0 && <span className="tabular-nums">({node.tokens.toLocaleString()} tokens)</span>}
          </h3>
          {agentState?.key === "completed" && node.output ? (
            <Markdown text={node.output} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-zinc-200" />
          ) : (
            <pre className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-[12px] leading-relaxed text-zinc-200">
              {node.output || node.preview || "(まだ出力はありません)"}
            </pre>
          )}
        </section>
      </div>
    </Drawer>
  );
}
