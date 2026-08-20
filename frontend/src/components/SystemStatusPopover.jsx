import { Zap, MemoryStick, Hourglass, WifiOff, Wifi } from "lucide-react";
import Popover from "./Popover.jsx";
import { fmtGB } from "../derive.js";

/**
 * ヘッダーのステータスピルから開くシステム状態(層1)。
 * 常設だった VRAMメーター / Active推論 / Queue数 の退避先。
 * active(t/s)は選択中RunのSSEからしか得られないため、その旨を明記する。
 */
export default function SystemStatusPopover({ open, onClose, gpu, health, active, queueCount, returnFocusRef }) {
  const used = gpu?.used_bytes ?? 0;
  const total = gpu?.total_bytes ?? 16 * 1024 ** 3;
  const ratio = total ? used / total : 0;
  const oom = ratio > 0.9;

  return (
    <Popover open={open} onClose={onClose} align="right" labelledBy="system-status-title"
             returnFocusRef={returnFocusRef}>
      <h3 id="system-status-title" className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
        システム状態
      </h3>

      {/* VRAM */}
      <div className="mb-3">
        <div className="mb-1 flex items-center gap-2 text-xs text-zinc-300">
          <MemoryStick size={13} className={oom ? "text-red-400" : "text-zinc-400"} />
          <span>VRAM</span>
          <span className={`ml-auto tabular-nums ${oom ? "font-semibold text-red-400" : "text-zinc-300"}`}>
            {gpu?.available ? `${fmtGB(used)} / ${fmtGB(total)} GB` : `-- / ${fmtGB(total)} GB`}
          </span>
        </div>
        <div className="h-2 w-full overflow-hidden rounded-full bg-zinc-900">
          <div
            className={`h-full rounded-full transition-all duration-500 ${
              oom ? "bg-red-500" : ratio > 0.7 ? "bg-yellow-500" : "bg-blue-500"
            }`}
            style={{ width: `${Math.min(100, ratio * 100)}%` }}
          />
        </div>
        {gpu?.models?.length > 0 && (
          <ul className="mt-1.5 space-y-0.5">
            {gpu.models.map((m) => (
              <li key={m.name} className="flex justify-between text-[11px] text-zinc-500">
                <span className="min-w-0 truncate">{m.name}</span>
                <span className="shrink-0 tabular-nums">{fmtGB(m.size_vram)} GB</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 選択中Runの推論 */}
      <div className="mb-3 flex items-center gap-2 text-xs">
        <Zap size={13} className={active ? "text-blue-400" : "text-zinc-600"} />
        {active ? (
          <span className="text-zinc-300">
            <b className="text-blue-400">{active.name}</b>
            <span className="ml-1 tabular-nums text-zinc-400">({active.tps.toFixed(1)} t/s)</span>
          </span>
        ) : (
          <span className="text-zinc-500">推論なし</span>
        )}
        <span className="ml-auto shrink-0 text-[10px] text-zinc-600">選択中Runの推論</span>
      </div>

      {/* キュー */}
      <div className="mb-3 flex items-center gap-2 text-xs text-zinc-300">
        <Hourglass size={13} className={queueCount > 0 ? "text-yellow-400" : "text-zinc-600"} />
        <span>GPU空き待ち</span>
        <span className={`ml-auto tabular-nums ${queueCount > 0 ? "text-yellow-400" : "text-zinc-500"}`}>
          {queueCount} 件
        </span>
      </div>

      {/* 接続状態 */}
      <div className="flex items-center gap-2 border-t border-zinc-700 pt-2 text-xs">
        {health?.ollama
          ? <><Wifi size={13} className="text-green-400" /><span className="text-zinc-400">Ollama 接続中</span></>
          : <><WifiOff size={13} className="text-red-400" /><span className="font-semibold text-red-400">Ollama 未接続</span></>}
      </div>
    </Popover>
  );
}
