import { useEffect, useRef, useState } from "react";
import {
  Square, RotateCcw, FileCog, Star, Timer, Coins, Wand2, Loader2, Info,
} from "lucide-react";
import Popover from "./Popover.jsx";
import { RUN_STATUS, MODE_LABEL, DELIVERABLE_LABEL, fmtElapsed } from "../derive.js";

/**
 * 選択中Runのサマリーヘッダー(1行)。
 *
 * 常設は「状態 / タスク1行 / 進捗 / 成果物 / 中断or再実行 / ⓘ」のみ。
 * メタ情報(進め方・成果物形式・モデル・判定理由・経過・トークン)と全文・設定変更は
 * ⓘの実行条件ポップオーバー(層1)へ退避した。タスク文クリックでも同じものが開く
 * (clamp-1 で読めない全文への最短導線)。
 */
export default function RunHeader({
  run, plan, progress, running, artifactCount, hasAnswer, now, aborting,
  onAbort, onRerun, onEditRerun, onOpenArtifacts,
}) {
  const [infoOpen, setInfoOpen] = useState(false);
  const infoBtnRef = useRef(null);
  const taskBtnRef = useRef(null);
  // ポップオーバーはタスク文とⓘの2トリガーから開けるため、閉じたとき
  // フォーカスを「実際に開いた方」へ返せるよう起点を覚えておく
  const openerRef = useRef(null);
  // Runを切り替えたらポップオーバーは閉じ直す(別Runの情報が開いたまま残らないように)
  useEffect(() => setInfoOpen(false), [run?.id]);
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
    <section className="flex items-center gap-2.5 border-b border-zinc-800 bg-zinc-900 px-4 py-2">
      <span
        className={`flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${meta.chip} ${meta.text}`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
        {meta.label}
      </span>

      <button
        ref={taskBtnRef}
        onClick={() => {
          openerRef.current = taskBtnRef.current;
          setInfoOpen(true);
        }}
        aria-haspopup="dialog"
        aria-expanded={infoOpen}
        className="min-w-0 flex-1 truncate text-left text-[13.5px] text-zinc-100 hover:text-blue-300"
        title="クリックで全文と実行条件を表示"
      >
        {run.task}
      </button>

      {/* 進捗(実行中のみ。code=iter/max、orchestra/swarm=完了サブタスク数) */}
      {total > 0 && (
        <span className="flex shrink-0 items-center gap-1.5">
          {/* バーは狭幅では畳むが、決定的進捗(n/m)の数字は常に残す */}
          <span className="hidden h-1.5 w-16 overflow-hidden rounded-full bg-zinc-800 sm:block">
            <span
              className={`block h-full rounded-full transition-all duration-500 ${
                running ? "bg-blue-500" : "bg-green-500"
              }`}
              style={{ width: `${ratio * 100}%` }}
            />
          </span>
          <span className="tabular-nums text-[10.5px] text-zinc-500">{done}/{total}</span>
        </span>
      )}

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
          <button
            onClick={onRerun}
            className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:border-blue-500 hover:text-blue-400"
            title="同じ設定で新しいRunを開始"
          >
            <RotateCcw size={10} /> 再実行
          </button>
        )}

        {/* ⓘ 実行条件(層1) */}
        <div className="relative">
          <button
            ref={infoBtnRef}
            onClick={() => {
              openerRef.current = infoBtnRef.current;
              setInfoOpen((v) => !v);
            }}
            aria-expanded={infoOpen}
            aria-haspopup="dialog"
            className={`rounded-md border p-1 transition-colors ${
              infoOpen ? "border-blue-500 text-blue-400" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
            }`}
            title="タスク全文と実行条件"
          >
            <Info size={13} />
          </button>
          <Popover open={infoOpen} onClose={() => setInfoOpen(false)} align="right"
                   labelledBy="run-info-title" returnFocusRef={openerRef}>
            <h3 id="run-info-title" className="sr-only">タスク全文と実行条件</h3>
            <p className="mb-2 max-h-40 overflow-y-auto whitespace-pre-wrap text-[12.5px] leading-snug text-zinc-100">
              {run.task}
            </p>
            <dl className="space-y-1.5 border-t border-zinc-700 pt-2 text-[11.5px]">
              <div className="flex items-center gap-2">
                <dt className="flex w-20 shrink-0 items-center gap-1 text-zinc-500">
                  <Wand2 size={11} className="text-blue-400" /> 進め方
                </dt>
                <dd className="text-zinc-200">
                  {modeLabel}
                  {dlvLabel ? ` / ${dlvLabel}` : ""}
                </dd>
              </div>
              <div className="flex items-center gap-2">
                <dt className="w-20 shrink-0 text-zinc-500">モデル</dt>
                <dd className="font-semibold text-blue-400">{modelTag}</dd>
              </div>
              {run.claude_review && (
                <div className="flex items-center gap-2">
                  <dt className="w-20 shrink-0 text-zinc-500">最終レビュー</dt>
                  <dd className="font-semibold text-orange-400">🤖 Claude</dd>
                </div>
              )}
              {reason && (
                <div className="flex items-start gap-2">
                  <dt className="w-20 shrink-0 text-zinc-500">判定理由</dt>
                  <dd className="text-zinc-400">{reason}</dd>
                </div>
              )}
              <div className="flex items-center gap-2">
                <dt className="flex w-20 shrink-0 items-center gap-1 text-zinc-500">
                  <Timer size={11} /> 経過
                </dt>
                <dd className="tabular-nums text-zinc-200">{fmtElapsed(elapsed)}</dd>
              </div>
              {run.tokens > 0 && (
                <div className="flex items-center gap-2">
                  <dt className="flex w-20 shrink-0 items-center gap-1 text-zinc-500">
                    <Coins size={11} /> トークン
                  </dt>
                  <dd className="tabular-nums text-zinc-200">{run.tokens.toLocaleString()} tok</dd>
                </div>
              )}
            </dl>
            {!live && (
              <button
                onClick={() => {
                  setInfoOpen(false);
                  onEditRerun();
                }}
                className="mt-2.5 flex w-full items-center justify-center gap-1.5 rounded-md border border-zinc-600 px-2 py-1.5 text-[11.5px] text-zinc-200 hover:border-blue-500 hover:text-blue-400"
              >
                <FileCog size={11} /> 設定を変更して再実行
              </button>
            )}
          </Popover>
        </div>
      </div>
    </section>
  );
}
