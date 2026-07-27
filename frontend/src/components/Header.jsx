import { useEffect, useState } from "react";
import { FlaskConical, MemoryStick, Zap, Hourglass, Square, AlertTriangle, WifiOff } from "lucide-react";
import { fmtGB } from "../derive.js";

/**
 * トップヘッダー: VRAMメーター / アクティブ推論 / キュー数 / 全停止。
 * gpu: /gpu のレスポンス, active: {name, tps}|null, queueCount: number
 */
export default function Header({ gpu, health, active, queueCount, onStopAll, stoppable }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);

  const used = gpu?.used_bytes ?? 0;
  const total = gpu?.total_bytes ?? 16 * 1024 ** 3;
  const ratio = total ? used / total : 0;
  const oom = ratio > 0.9;

  return (
    <header className="sticky top-0 z-30 flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-zinc-800 bg-zinc-900/95 px-4 py-2.5 backdrop-blur">
      <div className="flex items-center gap-2">
        <FlaskConical size={18} className="text-blue-400" />
        <h1 className="text-sm font-semibold tracking-wide text-zinc-100">ai-agent-lab</h1>
      </div>

      {/* VRAMメーター */}
      <div className="flex items-center gap-2 text-xs" title="Ollamaにロード中のモデルのVRAM使用量">
        <MemoryStick size={14} className={oom ? "text-red-400" : "text-zinc-400"} />
        <div className="h-2 w-36 overflow-hidden rounded-full bg-zinc-800">
          <div
            className={`h-full rounded-full transition-all duration-500 ${
              oom ? "bg-red-500" : ratio > 0.7 ? "bg-yellow-500" : "bg-blue-500"
            }`}
            style={{ width: `${Math.min(100, ratio * 100)}%` }}
          />
        </div>
        <span className={`tabular-nums ${oom ? "font-semibold text-red-400" : "text-zinc-300"}`}>
          {gpu?.available ? `${fmtGB(used)} / ${fmtGB(total)} GB` : `-- / ${fmtGB(total)} GB`}
        </span>
        {oom && (
          <span className="soft-pulse flex items-center gap-1 rounded-full border border-red-500 px-2 py-0.5 text-[11px] font-bold text-red-400">
            <AlertTriangle size={11} /> OOM Warning
          </span>
        )}
      </div>

      {/* アクティブ推論 */}
      <div className="flex items-center gap-1.5 text-xs text-zinc-300">
        <Zap size={14} className={active ? "text-blue-400" : "text-zinc-600"} />
        {active ? (
          <span>
            Active: <b className="text-blue-400">{active.name}</b>
            <span className="ml-1 tabular-nums text-zinc-400">({active.tps.toFixed(1)} t/s)</span>
          </span>
        ) : (
          <span className="text-zinc-500">GPU Idle</span>
        )}
      </div>

      {/* キュー数 */}
      <div className="flex items-center gap-1.5 text-xs text-zinc-300" title="GPU空き待ちのRun数">
        <Hourglass size={14} className={queueCount > 0 ? "text-yellow-400" : "text-zinc-600"} />
        <span>
          Queue: <b className={`tabular-nums ${queueCount > 0 ? "text-yellow-400" : "text-zinc-400"}`}>{queueCount}</b>
        </span>
      </div>

      {health && !health.ollama && (
        <div className="flex items-center gap-1.5 text-xs text-red-400">
          <WifiOff size={13} /> Ollama未接続
        </div>
      )}

      {/* 全停止 */}
      <button
        onClick={() => {
          if (!armed) return setArmed(true);
          setArmed(false);
          onStopAll();
        }}
        disabled={!stoppable}
        className={`ml-auto flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-bold transition-colors ${
          armed
            ? "border-red-500 bg-red-600 text-white"
            : stoppable
              ? "border-red-500/70 bg-transparent text-red-400 hover:bg-red-500/10"
              : "cursor-not-allowed border-zinc-700 text-zinc-600"
        }`}
        title="実行中・待機中の全Runを中断"
      >
        <Square size={12} fill="currentColor" />
        {armed ? "もう一度クリックで全停止" : "Stop All"}
      </button>
    </header>
  );
}
