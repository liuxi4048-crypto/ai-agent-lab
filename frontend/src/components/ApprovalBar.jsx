import { useEffect, useRef, useState } from "react";
import { ShieldAlert, Check, X, Loader2, AlertTriangle } from "lucide-react";
import { resolveApproval } from "../api.js";

// バックエンド(runs.py APPROVAL_TIMEOUT=900秒)の自動却下と揃えた表示用の目安値。
// receivedAt はUI側で観測した受付時刻の近似(サーバーはタイムスタンプを配信しないため)。
const APPROVAL_TIMEOUT_MS = 15 * 60 * 1000;

// 取り返しがつきにくい操作の目印。該当したら枠を赤くし、承認を2段階にする。
// denylist(tools.py)で弾かれないものだけがここに来る前提なので、警告目的の緩い検出でよい。
const DANGEROUS_RE =
  /(^|[\s;&|])(rm|rmdir|del|erase)\s+(-[a-z]*[rf]|\/[sq])|format\s|mkfs|shutdown|reboot|taskkill|reg\s+delete|git\s+push|npm\s+publish|pip\s+uninstall|\|\s*(sh|bash)\b|Invoke-WebRequest|curl\s+[^|]*\|/i;

function fmtRemain(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** run_command 実行前承認のバー。未応答の承認要求を上部に列挙する。 */
export default function ApprovalBar({ runId, approvals }) {
  const [now, setNow] = useState(Date.now());
  const [dismissed, setDismissed] = useState(() => new Set());
  const [rowError, setRowError] = useState({});  // aid -> エラー文言 | null
  const [busy, setBusy] = useState({});          // aid -> bool
  const [armed, setArmed] = useState(null);      // 破壊的コマンドの承認2段階確認
  const armTimer = useRef(null);
  useEffect(() => () => clearTimeout(armTimer.current), []);

  const visible = (approvals ?? []).filter((a) => !dismissed.has(a.aid));

  // 15分カウントダウン表示のための1秒tick(未応答が無ければ回さない)
  useEffect(() => {
    if (!visible.length) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [visible.length]);

  if (!visible.length) return null;

  const respond = async (a, approved) => {
    setBusy((s) => ({ ...s, [a.aid]: true }));
    setRowError((s) => ({ ...s, [a.aid]: null }));
    try {
      await resolveApproval(runId, a.aid, approved);
    } catch (e) {
      if (e.status === 409) {
        // 409 = サーバー側で既に解決済み(自動却下等)。表示を実態に合わせて消す
        setDismissed((s) => new Set(s).add(a.aid));
      } else {
        setRowError((s) => ({ ...s, [a.aid]: "送信失敗 — 再試行してください" }));
      }
    } finally {
      setBusy((s) => ({ ...s, [a.aid]: false }));
    }
  };

  const approve = (a, danger) => {
    if (danger && armed !== a.aid) {
      // 破壊的の可能性があるコマンドは1回目のクリックでは実行しない
      clearTimeout(armTimer.current);
      setArmed(a.aid);
      armTimer.current = setTimeout(() => setArmed(null), 3000);
      return;
    }
    setArmed(null);
    respond(a, true);
  };

  return (
    <div
      className="space-y-2 border-b border-yellow-500/30 bg-yellow-500/5 px-4 py-2.5"
      role="alert"
      aria-label="コマンド実行の承認要求"
    >
      {visible.map((a) => {
        const remainMs = APPROVAL_TIMEOUT_MS - (now - (a.receivedAt ?? now));
        const danger = DANGEROUS_RE.test(a.command || "");
        return (
          <div
            key={a.aid}
            className={`flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg ${
              danger ? "border border-red-500/60 bg-red-500/10 px-2 py-1.5" : ""
            }`}
          >
            <ShieldAlert size={15} className="soft-pulse shrink-0 text-yellow-400" />
            {/* コマンドは全文読めるようにする(承認するのに後半が読めないのは危険) */}
            <code className="min-w-0 flex-1 whitespace-pre-wrap break-all rounded bg-zinc-900 px-2 py-1 font-mono text-[12px] text-yellow-200">
              {a.command}
            </code>
            {danger && (
              <span className="flex shrink-0 items-center gap-1 rounded-full border border-red-500 px-2 py-0.5 text-[10.5px] font-bold text-red-300">
                <AlertTriangle size={11} /> 破壊的の可能性
              </span>
            )}
            <span
              className={`shrink-0 tabular-nums text-[11px] ${remainMs < 60_000 ? "font-bold text-red-400" : "text-zinc-500"}`}
              title="この時間を過ぎるとサーバー側で自動的に却下されます"
              aria-hidden="true"
            >
              {a.fromSnapshot
                ? "15分で自動却下(受付時刻不明)"
                : `自動却下まで ${fmtRemain(remainMs)}`}
            </span>
            <div className="flex gap-1.5">
              <button
                onClick={() => approve(a, danger)}
                disabled={busy[a.aid]}
                className={`flex items-center gap-1 rounded-md px-3 py-1 text-[12px] font-semibold text-white disabled:opacity-50 ${
                  armed === a.aid ? "bg-red-600 hover:bg-red-500" : "bg-green-600 hover:bg-green-500"
                }`}
              >
                {busy[a.aid] ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                {busy[a.aid] ? "送信中…" : armed === a.aid ? "本当に承認?" : "承認"}
              </button>
              <button
                onClick={() => respond(a, false)}
                disabled={busy[a.aid]}
                className="flex items-center gap-1 rounded-md border border-red-500/70 px-3 py-1 text-[12px] font-semibold text-red-400 hover:bg-red-500/10 disabled:opacity-50"
              >
                <X size={12} /> 却下
              </button>
            </div>
            <span className="w-full pl-6 text-[10px] text-zinc-500">cwd: {a.cwd}</span>
            {rowError[a.aid] && (
              <p className="w-full text-[11px] font-semibold text-red-400">{rowError[a.aid]}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
