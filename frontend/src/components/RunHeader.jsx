import { useEffect, useState } from "react";
import {
  Square, RotateCcw, FileCog, Star, Timer, Coins, Wand2, ChevronDown, ChevronUp, Loader2,
} from "lucide-react";
import { RUN_STATUS, MODE_LABEL, DELIVERABLE_LABEL, fmtElapsed } from "../derive.js";

/**
 * 選択中Runのサマリーヘッダー。
 *
 * 以前はタスク全文がどこにも表示されず(一覧チップのtruncateのみ)、Run単位の操作が
 * エージェントカード側に置かれていて「カードの再実行=Run全体の再実行」という誤解を
 * 生んでいた。ここに「何を頼んだか」と「Runに対する操作」を集約する。
 */
export default function RunHeader({
  run, plan, progress, running, artifactCount, hasAnswer, now, aborting,
  onAbort, onRerun, onEditRerun, onOpenArtifacts,
}) {
  const [expanded, setExpanded] = useState(false);
  // Runを切り替えたら「全文を表示」は畳み直す(別Runの長文が開いたまま残らないように)
  useEffect(() => setExpanded(false), [run?.id]);
  if (!run) return null;

  const meta = RUN_STATUS[run.status] ?? RUN_STATUS.cancelled;
  const live = run.status === "running" || run.status === "queued";
  const elapsed = run.created_at
    ? (run.finished_at ?? now / 1000) - run.created_at
    : null;
  const [done, total] = progress ?? [];
  const ratio = total ? Math.min(1, done / total) : 0;

  // 進め方・成果物・モデル・判定理由。plan(実行時レスポンス)が無い過去Runは run から組む
  const modeLabel = MODE_LABEL[plan?.mode ?? run.mode] ?? (plan?.mode ?? run.mode);
  const dlv = plan?.deliverable ?? run.deliverable;
  const dlvLabel = DELIVERABLE_LABEL[dlv];
  const modelTag = plan?.model_tag ?? run.model_tag ?? run.model;
  const reason = plan?.reason;

  return (
    <section className="border-b border-zinc-800 bg-zinc-900/40 px-4 py-2.5">
      <div className="flex items-start gap-3">
        <span
          className={`mt-0.5 flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${meta.chip} ${meta.text}`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
          {meta.label}
        </span>

        <div className="min-w-0 flex-1">
          <p
            className={`text-[13.5px] leading-snug text-zinc-100 ${expanded ? "" : "line-clamp-2"}`}
          >
            {run.task}
          </p>
          {(run.task || "").length > 90 && (
            <button
              onClick={() => setExpanded((v) => !v)}
              className="mt-0.5 flex items-center gap-0.5 text-[10.5px] text-zinc-500 hover:text-zinc-300"
            >
              {expanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
              {expanded ? "折りたたむ" : "全文を表示"}
            </button>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {/* 成果物ファイルが無いRun(調査・考察/推敲)でも最終レポートはここから開く */}
          {(artifactCount > 0 || hasAnswer) && (
            <button
              onClick={onOpenArtifacts}
              className="flex items-center gap-1 rounded-md border border-yellow-600/70 bg-yellow-500/10 px-2 py-1 text-[11px] font-semibold text-yellow-300 hover:bg-yellow-500/20"
              title={artifactCount > 0 ? "生成された成果物を開く" : "最終レポートを開く"}
            >
              <Star size={11} fill="currentColor" />
              {artifactCount > 0 ? `成果物 ${artifactCount}` : "レポート"}
            </button>
          )}
          {live ? (
            <button
              onClick={onAbort}
              disabled={aborting}
              className="flex items-center gap-1 rounded-md border border-red-500/60 px-2 py-1 text-[11px] text-red-400 hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-60"
              title="このRunを中断する(推論は途中再開できないため、再開ではなくコンテキストを保持した再実行になります)"
            >
              {aborting
                ? <><Loader2 size={10} className="animate-spin" /> 中断中…</>
                : <><Square size={10} fill="currentColor" /> 中断</>}
            </button>
          ) : (
            <>
              <button
                onClick={onRerun}
                className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:border-blue-500 hover:text-blue-400"
                title="同じ設定で新しいRunを開始"
              >
                <RotateCcw size={10} /> 再実行
              </button>
              <button
                onClick={onEditRerun}
                className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:border-blue-500 hover:text-blue-400"
                title="タスク・モデル等を変更して再実行"
              >
                <FileCog size={10} /> 設定変更
              </button>
            </>
          )}
        </div>
      </div>

      {/* メタ情報(進め方・成果物形式・モデル・自動判定理由・経過・トークン) */}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
        <span className="flex items-center gap-1">
          <Wand2 size={11} className="text-blue-400" />
          <span className="text-zinc-300">
            {modeLabel}
            {dlvLabel ? ` / ${dlvLabel}` : ""}
          </span>
        </span>
        <span className="font-semibold text-blue-400">{modelTag}</span>
        {run.claude_review && <span className="font-semibold text-orange-400">+🤖Claudeレビュー</span>}
        {reason && <span className="text-zinc-600">({reason})</span>}
        <span className="flex items-center gap-1 tabular-nums">
          <Timer size={11} /> {fmtElapsed(elapsed)}
        </span>
        {run.tokens > 0 && (
          <span className="flex items-center gap-1 tabular-nums">
            <Coins size={11} /> {run.tokens.toLocaleString()} tok
          </span>
        )}
        {run.queue_reason && run.status === "queued" && (
          <span className="text-yellow-500">{run.queue_reason}</span>
        )}
      </div>

      {/* 進捗バー(実行中のみ。code=iter/max、orchestra/swarm=完了サブタスク数) */}
      {total > 0 && (
        <div className="mt-2 flex items-center gap-2">
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-800">
            <div
              className={`h-full rounded-full transition-all duration-500 ${
                running ? "bg-blue-500" : "bg-green-500"
              }`}
              style={{ width: `${ratio * 100}%` }}
            />
          </div>
          <span className="shrink-0 tabular-nums text-[10.5px] text-zinc-500">
            {done}/{total}
          </span>
        </div>
      )}
    </section>
  );
}
