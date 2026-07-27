import { useEffect, useState } from "react";
import { ChevronDown, Pencil, Play, Loader2 } from "lucide-react";

const MODES = [
  { value: "code", label: "code (単独コーディング)" },
  { value: "swarm-code", label: "swarm-code (並列コーディング)" },
  { value: "orchestra", label: "orchestra (計画→並列→統合)" },
  { value: "critique", label: "critique (執筆⇄レビュー)" },
];

/**
 * 折りたたみ式タスク入力ペイン。実行開始で自動的に1行サマリーへ最小化。
 * prefill: [📝 設定変更して再実行] からの流し込み {task, mode, model, ...}
 */
export default function TaskInput({ models, onStart, running, prefill }) {
  const [open, setOpen] = useState(true);
  const [task, setTask] = useState("");
  const [mode, setMode] = useState("code");
  const [model, setModel] = useState("auto");
  const [critique, setCritique] = useState(false);
  const [approve, setApprove] = useState(true);
  const [maxIter, setMaxIter] = useState(18);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!prefill) return;
    setTask(prefill.task ?? "");
    setMode(prefill.mode ?? "code");
    setModel(prefill.model ?? "auto");
    setCritique(!!prefill.critique);
    setApprove(prefill.approve ?? true);
    setMaxIter(prefill.max_iter ?? 18);
    setOpen(true);
  }, [prefill]);

  const submit = async () => {
    if (!task.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onStart({ task: task.trim(), mode, model, critique, approve, max_iter: maxIter });
      setOpen(false); // 実行開始 → 1行サマリーへ自動最小化
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const modelOptions = models?.models ?? [];

  if (!open) {
    return (
      <div className="flex items-center gap-3 border-b border-zinc-800 bg-zinc-900/60 px-4 py-2">
        <ChevronDown size={14} className="-rotate-90 text-zinc-500" />
        <span className="min-w-0 flex-1 truncate text-xs text-zinc-400">
          <span className="mr-2 rounded bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-300">{mode}</span>
          {task || "(タスク未入力)"}
        </span>
        <button
          onClick={() => setOpen(true)}
          className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:border-blue-500 hover:text-blue-400"
          title="タスクを再編集"
        >
          <Pencil size={11} /> 編集
        </button>
      </div>
    );
  }

  return (
    <div className="border-b border-zinc-800 bg-zinc-900/60 px-4 py-3">
      <div className="flex flex-wrap items-start gap-2">
        <textarea
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
          }}
          rows={2}
          placeholder="全体タスクを入力 (Ctrl+Enterで実行)…"
          className="min-w-60 flex-1 resize-none rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-blue-500"
        />
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          className="rounded-md border border-zinc-700 bg-zinc-950 px-2 py-2 text-xs text-zinc-200 outline-none focus:border-blue-500"
        >
          {MODES.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="max-w-52 rounded-md border border-zinc-700 bg-zinc-950 px-2 py-2 text-xs text-zinc-200 outline-none focus:border-blue-500"
        >
          <option value="auto">auto (自動選択)</option>
          {modelOptions.map((m) => (
            <option key={m.key} value={m.key} disabled={!m.installed}>
              {m.key}{m.installed ? "" : " (未導入)"}
            </option>
          ))}
        </select>
        <button
          onClick={submit}
          disabled={busy || !task.trim()}
          className="flex items-center gap-1.5 rounded-md bg-green-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-green-500 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-500"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
          実行
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-zinc-400">
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={approve} onChange={(e) => setApprove(e.target.checked)}
                 className="accent-blue-500" />
          コマンド実行前に承認
        </label>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={critique} onChange={(e) => setCritique(e.target.checked)}
                 className="accent-blue-500" />
          完了後レビュー(critique)
        </label>
        <label className="flex items-center gap-1.5">
          最大イテレーション
          <input
            type="number" min={1} max={60} value={maxIter}
            onChange={(e) => setMaxIter(Number(e.target.value) || 18)}
            className="w-14 rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 text-zinc-200 outline-none focus:border-blue-500"
          />
        </label>
        {running && (
          <button onClick={() => setOpen(false)} className="ml-auto text-zinc-500 hover:text-zinc-300">
            折りたたむ ▲
          </button>
        )}
      </div>
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </div>
  );
}
