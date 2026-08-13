import { useEffect, useRef, useState } from "react";
import { Trash2 } from "lucide-react";

const RUN_DOT = {
  running: "bg-blue-400 soft-pulse",
  queued: "bg-yellow-400",
  done: "bg-green-400",
  error: "bg-red-400",
  cancelled: "bg-zinc-500",
  interrupted: "bg-zinc-600",
};

// 内部のモード名は出さず、何をしたかが分かる日本語で見せる
const MODE_LABEL = {
  code: "制作", "swarm-code": "並列制作", orchestra: "調査・考察", critique: "推敲",
};
const DELIVERABLE_LABEL = {
  html: "HTMLアプリ", exe: "exe", script: "スクリプト",
};

/** Run切り替えストリップ(新しい順)。選択中Runのエージェント群がワークスペースに出る。 */
export default function RunPicker({ runs, currentId, onSelect, onDelete }) {
  // 削除は誤操作防止のため2段階確認(1回目クリックで「削除?」に変化 → 3秒以内の再クリックで実行)
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

  if (!runs.length) return null;
  return (
    <div className="flex gap-2 overflow-x-auto border-b border-zinc-800/70 bg-zinc-950 px-4 py-2">
      {runs.map((r) => {
        const active = r.id === currentId;
        const finished = ["done", "error", "cancelled", "interrupted"].includes(r.status);
        return (
          <div
            key={r.id}
            onClick={() => onSelect(r.id)}
            title={r.task}
            className={`group flex shrink-0 cursor-pointer items-center gap-2 rounded-full border px-3 py-1 text-[12px] transition-colors ${
              active
                ? "border-blue-500 bg-blue-500/10 text-zinc-100"
                : "border-zinc-800 bg-zinc-900 text-zinc-400 hover:border-zinc-600"
            }`}
          >
            <span className={`h-2 w-2 shrink-0 rounded-full ${RUN_DOT[r.status] ?? "bg-zinc-500"}`} />
            <span className="max-w-48 truncate">{r.task}</span>
            {/* メインエージェントが決めた進め方・成果物と、実際に使ったモデル名 */}
            <span className="shrink-0 text-[10px] text-zinc-600">
              {[r.model_tag || r.model, MODE_LABEL[r.mode] ?? r.mode,
                DELIVERABLE_LABEL[r.deliverable]].filter(Boolean).join(" · ")}
            </span>
            {r.pending_approvals > 0 && (
              <span className="soft-pulse shrink-0" title={`承認待ち ${r.pending_approvals}件`}>🔔</span>
            )}
            {finished && (
              <button
                onClick={(e) => handleDeleteClick(e, r.id)}
                className={`shrink-0 flex items-center gap-1 rounded-full px-1.5 text-[10px] transition-colors ${
                  armedId === r.id
                    ? "bg-red-600 font-semibold text-white"
                    : "text-zinc-600 opacity-60 hover:text-red-400 hover:opacity-100"
                }`}
                title={armedId === r.id ? "3秒以内にもう一度クリックで削除" : "Run記録を削除"}
              >
                <Trash2 size={11} />
                {armedId === r.id && "削除?"}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
