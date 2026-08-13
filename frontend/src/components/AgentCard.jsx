import {
  Zap, Hourglass, Clock, Wrench, CheckCircle2, XCircle, Ban, Square,
  AlertTriangle, RotateCcw, FileCog, BookOpen, Timer,
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
 * デフォルトは名前/役割/状態/依存/経過時間/最新1行のみ。ログ全文はサイドドロワーへ。
 */
export default function AgentCard({
  node, agentState, dep, depState, tps, sinceMs, now,
  onOpen, onAbort, onRerun, onEditRerun,
}) {
  const meta = AGENT_STATES[agentState.key];
  const Icon = STATE_ICON[agentState.key];
  const isLive = ["active", "tool", "stalled"].includes(agentState.key);
  const isFinished = ["completed", "failed", "aborted", "canceled"].includes(agentState.key);
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
      onClick={onOpen}
      className={`group flex cursor-pointer flex-col gap-2 rounded-xl border bg-zinc-900 p-3.5 transition-colors hover:border-blue-500/70 ${
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
          {loading ? "モデル準備中(ロード/プロンプト評価)" : meta.label}
          {agentState.key === "queued" && agentState.pos != null && ` #${agentState.pos}`}
        </span>
      </div>

      {/* 2行目: 依存バッジ + 経過時間 + t/s */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
        {dep ? (
          <span className="flex items-center gap-1 truncate">
            Depends on:
            <span className={`flex items-center gap-1 rounded-full border border-zinc-700 px-1.5 py-px ${depState ? AGENT_STATES[depState.key].text : "text-zinc-400"}`}>
              <span className={`inline-block h-1.5 w-1.5 rounded-full ${depState ? AGENT_STATES[depState.key].dot : "bg-zinc-500"}`} />
              <span className="max-w-36 truncate">{dep.title}</span>
            </span>
          </span>
        ) : (
          <span className="text-zinc-600">依存なし</span>
        )}
        <span className="flex items-center gap-1 tabular-nums">
          <Timer size={11} /> {fmtElapsed(elapsed)}
        </span>
        {agentState.key === "active" && tps != null && (
          <span className="tabular-nums text-blue-400">{tps.toFixed(1)} t/s</span>
        )}
        {node.tokens > 0 && <span className="tabular-nums">{node.tokens.toLocaleString()} tok</span>}
        {node.progress && (
          <span className="tabular-nums">進捗 {node.progress[0]}/{node.progress[1]}</span>
        )}
        {ctxLevel !== "none" && (
          <span
            className={`rounded-full border px-1.5 py-0.5 text-[10px] font-bold tabular-nums ${CTX_PILL_CLASS[ctxLevel]}`}
            title="コンテキスト充填率(上限に近いと要約や打ち切りが起きやすくなります)"
          >
            ctx {Math.round(node.ctx_fill * 100)}%
          </span>
        )}
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
        <p className="px-0.5 font-mono text-[11px] text-zinc-700">
          {agentState.key === "waiting" ? "前段の完了を待機中…" : agentState.key === "queued" ? "GPUの空きを待機中…" : " "}
        </p>
      )}

      {/* アクション */}
      <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
        {isLive && (
          <button
            onClick={onAbort}
            className="flex items-center gap-1 rounded-md border border-red-500/60 px-2 py-1 text-[11px] text-red-400 hover:bg-red-500/10"
            title="推論の性質上「一時停止」はできません。中断後はコンテキストを保持した再実行になります"
          >
            <Square size={10} fill="currentColor" /> 中断
          </button>
        )}
        {isFinished && (
          <>
            <button
              onClick={onRerun}
              className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:border-blue-500 hover:text-blue-400"
              title="同条件でRunを再実行(元の設定を引き継いで新しいRunを開始)"
            >
              <RotateCcw size={10} /> 再実行
            </button>
            <button
              onClick={onEditRerun}
              className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:border-blue-500 hover:text-blue-400"
              title="タスク・モデル等を変更して再実行"
            >
              <FileCog size={10} /> 設定変更して再実行
            </button>
          </>
        )}
        <button
          onClick={onOpen}
          className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-zinc-500 opacity-60 transition-opacity hover:text-blue-400 group-hover:opacity-100"
        >
          <BookOpen size={11} /> 詳細を表示
        </button>
      </div>
    </div>
  );
}
