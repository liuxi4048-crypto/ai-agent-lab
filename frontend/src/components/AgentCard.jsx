import {
  Zap, Hourglass, Clock, Wrench, CheckCircle2, XCircle, Ban, Square,
  AlertTriangle, BookOpen, Timer,
} from "lucide-react";
import { AGENT_STATES, KIND_LABEL, fmtElapsed, lastLine, isModelLoading, ctxFillLevel } from "../derive.js";

const CTX_PILL_CLASS = {
  warn: "border-yellow-500 text-yellow-400",
  danger: "border-red-500 text-red-400",
};

const STATE_ICON = {
  active: Zap, queued: Hourglass, waiting: Clock, tool: Wrench, completed: CheckCircle2,
  failed: XCircle, canceled: Ban, aborted: Square, stalled: AlertTriangle,
};

/**
 * エージェントカード(コンパクト設計)。
 * 表示は名前/役割/状態/依存/経過/最新1行のみ。ログ全文はサイドドロワーへ。
 *
 * Run単位の操作(中断・再実行)はここには置かない。カード=1エージェントなのに
 * 「カードの再実行」がRun全体の再実行になり誤解を生んでいたため、RunHeaderへ集約した。
 */
export default function AgentCard({
  node, agentState, dep, depState, tps, sinceMs, now, onOpen,
}) {
  const meta = AGENT_STATES[agentState.key];
  const Icon = STATE_ICON[agentState.key];
  const isLive = ["active", "tool", "stalled"].includes(agentState.key);
  const loading = agentState.key === "active" && isModelLoading(node, now);
  const ctxLevel = ctxFillLevel(node.ctx_fill);
  const stalledSec = sinceMs != null ? Math.floor(sinceMs / 1000) : null;

  const elapsed =
    node.started_at != null
      ? (node.finished_at ?? now / 1000) - node.started_at
      : null;

  const line = lastLine(node);

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`${node.title} — ${meta.label}。詳細ログを開く`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      className={`group flex cursor-pointer flex-col gap-2 rounded-xl border bg-zinc-900 p-3.5 transition-colors hover:border-blue-500/70 focus-visible:border-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40 ${
        agentState.key === "stalled" ? "stall-blink border-orange-500" : meta.border
      }`}
    >
      {/* 1行目: 名前 + 役割バッジ + 状態 */}
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-zinc-100">
          {node.title}
        </span>
        <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
          {KIND_LABEL[node.kind] ?? node.kind}
        </span>
        <span className={`flex shrink-0 items-center gap-1 rounded-full bg-zinc-800/80 px-2 py-0.5 text-[11px] font-semibold ${meta.text} ${isLive ? "soft-pulse" : ""}`}>
          <Icon size={11} />
          {loading ? "モデル準備中" : meta.label}
          {agentState.key === "queued" && agentState.pos != null && ` #${agentState.pos}`}
        </span>
      </div>

      {/* 2行目: 依存 + 経過 + t/s + トークン + 進捗 + ctx + 詳細導線 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
        {dep ? (
          <span className="flex min-w-0 items-center gap-1">
            <span className="shrink-0 text-zinc-600">前段:</span>
            <span className={`flex min-w-0 items-center gap-1 rounded-full border border-zinc-700 px-1.5 py-px ${depState ? AGENT_STATES[depState.key].text : "text-zinc-400"}`}>
              <span className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${depState ? AGENT_STATES[depState.key].dot : "bg-zinc-500"}`} />
              <span className="max-w-32 truncate">{dep.title}</span>
            </span>
          </span>
        ) : null}
        <span className="flex items-center gap-1 tabular-nums">
          <Timer size={11} /> {fmtElapsed(elapsed)}
        </span>
        {agentState.key === "active" && tps != null && tps > 0 && (
          <span className="tabular-nums font-semibold text-blue-400">{tps.toFixed(1)} t/s</span>
        )}
        {node.tokens > 0 && <span className="tabular-nums">{node.tokens.toLocaleString()} tok</span>}
        {node.progress && (
          <span className="tabular-nums">{node.progress[0]}/{node.progress[1]}</span>
        )}
        {ctxLevel !== "none" && (
          <span
            className={`rounded-full border px-1.5 py-0.5 text-[10px] font-bold tabular-nums ${CTX_PILL_CLASS[ctxLevel]}`}
            title="コンテキスト充填率(上限に近いと要約や打ち切りが起きやすくなります)"
          >
            ctx {Math.round(node.ctx_fill * 100)}%
          </span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-1 text-zinc-600 transition-colors group-hover:text-blue-400">
          <BookOpen size={11} /> 詳細
        </span>
      </div>

      {/* Stalled警告 / エラーサマリー / 最新1行ログ */}
      {agentState.key === "stalled" ? (
        <p className="flex items-center gap-1.5 truncate rounded bg-orange-500/10 px-2 py-1 font-mono text-[11px] text-orange-400">
          <AlertTriangle size={11} className="shrink-0" />
          {stalledSec != null ? `${stalledSec}秒間トークンが来ていません` : "トークンが来ていません"} — ログを確認してください
        </p>
      ) : agentState.key === "failed" ? (
        <p className="truncate rounded bg-red-500/10 px-2 py-1 font-mono text-[11px] text-red-400">
          {(node.output || "エラー詳細なし").split("\n")[0]}
        </p>
      ) : agentState.key === "canceled" ? (
        <p className="truncate px-0.5 font-mono text-[11px] text-zinc-500">
          依存先の失敗により自動キャンセルされました
        </p>
      ) : line ? (
        <p className="truncate px-0.5 font-mono text-[11px] text-zinc-500">{line}</p>
      ) : (
        <p className="px-0.5 font-mono text-[11px] text-zinc-500">
          {agentState.key === "waiting" ? "前段の完了を待機中…" : agentState.key === "queued" ? "GPUの空きを待機中…" : " "}
        </p>
      )}
    </div>
  );
}
