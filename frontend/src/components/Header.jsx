import { useEffect, useRef, useState } from "react";
import {
  FlaskConical, Zap, Square, AlertTriangle, WifiOff, Menu, Plus, Gauge, ChevronDown,
} from "lucide-react";
import SystemStatusPopover from "./SystemStatusPopover.jsx";

/**
 * トップヘッダー。常設は「ロゴ / ステータスピル」だけに絞り、
 * 要対応のもの(OOM・Ollama未接続・承認待ち・全停止)は条件を満たすときだけ出す。
 * VRAMメーター・Active推論・Queue数の詳細はステータスピルのポップオーバー(層1)へ。
 *
 * runningCount/queueCount は runs 由来(全Run)。選択中RunのSSEから作る active とは
 * 集計元が違うことに注意(draft中でも実行中件数が正しく出るのはこのため)。
 */
export default function Header({
  gpu, health, active, queueCount, runningCount, onStopAll, stoppable,
  pendingApprovals = 0, onJumpToPending, onToggleSidebar, sidebarOpen = false, onNewTask,
}) {
  const [armed, setArmed] = useState(false);
  const [statusOpen, setStatusOpen] = useState(false);
  const pillRef = useRef(null);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);

  const used = gpu?.used_bytes ?? 0;
  const total = gpu?.total_bytes ?? 16 * 1024 ** 3;
  const oom = total ? used / total > 0.9 : false;

  return (
    <header className="z-30 flex h-11 shrink-0 items-center gap-x-3 border-b border-zinc-800 bg-zinc-900 px-3">
      <button
        onClick={onToggleSidebar}
        className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 lg:hidden"
        title={sidebarOpen ? "タスク一覧を閉じる" : "タスク一覧を開く"}
        aria-expanded={!!sidebarOpen}
        aria-label="タスク一覧の開閉"
      >
        <Menu size={16} />
      </button>
      <div className="flex items-center gap-2">
        <FlaskConical size={18} className="text-blue-400" />
        <h1 className="text-sm font-semibold tracking-wide text-zinc-100">ai-agent-lab</h1>
      </div>

      {/* 狭幅: サイドバーを開かずに新規タスクへ */}
      <button
        onClick={onNewTask}
        className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-blue-400 lg:hidden"
        title="新しいタスク(N)"
        aria-label="新しいタスク"
      >
        <Plus size={16} />
      </button>

      {/* 要対応バッジ群(条件を満たすときだけ現れる) */}
      {oom && (
        <span className="soft-pulse flex items-center gap-1 rounded-full border border-red-500 px-2 py-0.5 text-[11px] font-bold text-red-400">
          <AlertTriangle size={11} /> VRAM残りわずか
        </span>
      )}
      {health && !health.ollama && (
        <span className="flex items-center gap-1.5 text-xs font-semibold text-red-400">
          <WifiOff size={13} /> Ollama未接続
        </span>
      )}
      {pendingApprovals > 0 && (
        <button
          onClick={onJumpToPending}
          className="soft-pulse flex items-center gap-1.5 rounded-full border border-yellow-500 bg-yellow-500/10 px-2.5 py-1 text-xs font-bold text-yellow-300 hover:bg-yellow-500/20"
          title="承認待ちのコマンドがあります"
        >
          🔔 承認待ち {pendingApprovals}
        </button>
      )}

      {/* ステータスピル: 稼働の要約 + クリックでシステム状態(層1) */}
      <div className="relative ml-auto">
        <button
          ref={pillRef}
          onClick={() => setStatusOpen((v) => !v)}
          aria-expanded={statusOpen}
          aria-haspopup="dialog"
          className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors ${
            statusOpen
              ? "border-blue-500 text-blue-400"
              : "border-zinc-700 text-zinc-300 hover:border-zinc-500"
          }`}
          title="システム状態(VRAM・推論・キュー)"
        >
          {runningCount > 0 ? (
            <>
              <Zap size={12} className="text-blue-400" />
              <span className="tabular-nums font-semibold text-blue-400">{runningCount}</span>
              <span className="hidden sm:inline">実行中</span>
            </>
          ) : (
            <Gauge size={13} className="text-zinc-500" />
          )}
          {queueCount > 0 && (
            <span className="tabular-nums text-yellow-400">⏳{queueCount}</span>
          )}
          <ChevronDown size={11} className="text-zinc-500" />
        </button>
        <SystemStatusPopover
          open={statusOpen}
          onClose={() => setStatusOpen(false)}
          gpu={gpu}
          health={health}
          active={active}
          queueCount={queueCount}
          returnFocusRef={pillRef}
        />
      </div>

      {/* 全停止: 止められるものがあるときだけ現れる(2段階クリック) */}
      {stoppable && (
        <button
          onClick={() => {
            if (!armed) return setArmed(true);
            setArmed(false);
            onStopAll();
          }}
          className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-bold transition-colors ${
            armed
              ? "border-red-500 bg-red-600 text-white"
              : "border-red-500/70 bg-transparent text-red-400 hover:bg-red-500/10"
          }`}
          title="実行中・待機中の全Runを中断"
        >
          <Square size={12} fill="currentColor" />
          <span className={armed ? "" : "hidden sm:inline"}>
            {armed ? "もう一度クリックで全停止" : "全停止"}
          </span>
        </button>
      )}
    </header>
  );
}
