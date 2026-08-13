import { useEffect, useMemo, useRef, useState } from "react";
import { Plus, Search, Trash2, X } from "lucide-react";
import { RUN_STATUS, MODE_LABEL, DELIVERABLE_LABEL, fmtAgo } from "../derive.js";

/**
 * 左サイドバー: 新規タスク導線 + Run一覧(縦)。
 *
 * 横並びチップだったRun切り替えを縦リストへ移した。履歴が20件を超えると横スクロールでは
 * 探せなくなるため、検索と「実行中/履歴」のグループ分けを持たせている。
 * lg未満ではオフキャンバス(ハンバーガーで開閉)、lg以上は常設カラム。
 */
export default function Sidebar({ runs, currentId, onSelect, onDelete, onNew, open, onClose }) {
  const [query, setQuery] = useState("");
  // 削除は誤操作防止の2段階確認(1回目で「削除?」→3秒以内の再クリックで実行)
  const [armedId, setArmedId] = useState(null);
  const armTimer = useRef(null);
  useEffect(() => () => clearTimeout(armTimer.current), []);

  const handleDeleteClick = (e, id) => {
    e.stopPropagation();
    clearTimeout(armTimer.current);
    if (armedId === id) {
      setArmedId(null);
      onDelete(id);
      return;
    }
    setArmedId(id);
    armTimer.current = setTimeout(() => setArmedId(null), 3000);
  };

  const { live, history } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const hit = q ? runs.filter((r) => (r.task || "").toLowerCase().includes(q)) : runs;
    return {
      live: hit.filter((r) => r.status === "running" || r.status === "queued"),
      history: hit.filter((r) => r.status !== "running" && r.status !== "queued"),
    };
  }, [runs, query]);

  const row = (r) => {
    const meta = RUN_STATUS[r.status] ?? RUN_STATUS.cancelled;
    const selected = r.id === currentId;
    const finished = !["running", "queued"].includes(r.status);
    return (
      <li key={r.id}>
        <div
          role="button"
          tabIndex={0}
          onClick={() => {
            onSelect(r.id);
            onClose?.();
          }}
          onKeyDown={(e) => {
            // 行の内側にある削除ボタンのEnter/Spaceを奪わない。
            // preventDefault すると <button> の既定動作(click)が消え、削除できないまま
            // Runが切り替わってしまう(キーボードだけで削除する手段が無くなる)
            if (e.target !== e.currentTarget) return;
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onSelect(r.id);
              onClose?.();
            }
          }}
          title={r.task}
          className={`group relative cursor-pointer border-l-2 px-3 py-2 transition-colors ${
            selected
              ? "border-blue-500 bg-blue-500/10"
              : "border-transparent hover:border-zinc-600 hover:bg-zinc-800/60"
          }`}
        >
          <div className="flex items-start gap-2">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${meta.dot}`} title={meta.label} />
            <p className={`min-w-0 flex-1 line-clamp-2 text-[12.5px] leading-snug ${selected ? "text-zinc-100" : "text-zinc-300"}`}>
              {r.task}
            </p>
            {r.pending_approvals > 0 && (
              <span className="soft-pulse shrink-0 text-[13px]" title={`承認待ち ${r.pending_approvals}件`}>
                🔔
              </span>
            )}
          </div>
          <div className="mt-1 flex items-center gap-1.5 pl-4 text-[10.5px] text-zinc-500">
            <span className={meta.text}>{meta.label}</span>
            <span className="text-zinc-700">·</span>
            <span className="min-w-0 truncate">
              {[MODE_LABEL[r.mode] ?? r.mode, DELIVERABLE_LABEL[r.deliverable]].filter(Boolean).join(" / ")}
            </span>
            <span className="ml-auto shrink-0 tabular-nums">{fmtAgo(r.created_at)}</span>
            {finished && (
              <button
                onClick={(e) => handleDeleteClick(e, r.id)}
                className={`shrink-0 rounded px-1 transition-colors ${
                  armedId === r.id
                    ? "bg-red-600 font-semibold text-white"
                    : "text-zinc-400 opacity-0 hover:text-red-400 focus-visible:opacity-100 group-hover:opacity-100"
                }`}
                title={armedId === r.id ? "3秒以内にもう一度クリックで削除" : "Run記録を削除"}
              >
                {armedId === r.id ? "削除?" : <Trash2 size={11} />}
              </button>
            )}
          </div>
          <p className="pl-4 text-[10px] text-zinc-500">{r.model_tag || r.model}</p>
        </div>
      </li>
    );
  };

  const group = (label, items) =>
    items.length > 0 && (
      <>
        <li className="sticky top-0 z-10 flex items-center gap-2 bg-zinc-900/95 px-3 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 backdrop-blur">
          {label}
          <span className="tabular-nums text-zinc-400">{items.length}</span>
        </li>
        {items.map(row)}
      </>
    );

  return (
    <>
      {/* lg未満: オフキャンバス時の暗転 */}
      <div
        onClick={onClose}
        className={`fixed inset-0 z-30 bg-black/50 transition-opacity lg:hidden ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      {/* 閉じている間は max-lg:invisible でフォーカス順から外す。translate だけで画面外へ
          ずらしても Tab は入ってしまい、狭幅では見えないサイドバー内の要素(Run行40件+
          削除ボタン)を90回以上通過しないと本文へ到達できなくなる */}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-72 shrink-0 transform flex-col border-r border-zinc-800 bg-zinc-900 transition-transform duration-200 lg:static lg:inset-auto lg:z-auto lg:visible lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full max-lg:invisible"
        }`}
      >
        <div className="flex items-center gap-2 p-3">
          <button
            onClick={() => {
              onNew();
              onClose?.();
            }}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-blue-600 px-3 py-2 text-[13px] font-semibold text-white transition-colors hover:bg-blue-500"
            title="新しいタスクを入力する(N)"
          >
            <Plus size={14} /> 新しいタスク
          </button>
          <button
            onClick={onClose}
            className="rounded-md p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 lg:hidden"
            title="閉じる"
          >
            <X size={16} />
          </button>
        </div>

        <div className="relative px-3 pb-2">
          <Search size={12} className="absolute left-5 top-1/2 -translate-y-1/2 text-zinc-600" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="タスクを検索…"
            className="w-full rounded-md border border-zinc-800 bg-zinc-950 py-1.5 pl-7 pr-2 text-[12px] text-zinc-200 placeholder-zinc-600 outline-none focus:border-blue-500"
          />
        </div>

        <ul className="min-h-0 flex-1 overflow-y-auto pb-3">
          {group("実行中・待機中", live)}
          {group("履歴", history)}
          {!live.length && !history.length && (
            <li className="px-3 py-6 text-center text-[12px] text-zinc-500">
              {query ? "一致するタスクがありません" : "まだタスクがありません"}
            </li>
          )}
        </ul>
      </aside>
    </>
  );
}
