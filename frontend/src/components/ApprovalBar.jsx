import { useEffect, useState } from "react";
import { ShieldAlert, Check, X } from "lucide-react";
import { resolveApproval } from "../api.js";

// バックエンド(runs.py APPROVAL_TIMEOUT=900秒)の自動却下と揃えた表示用の目安値。
// receivedAt はUI側で観測した受付時刻の近似(サーバーはタイムスタンプを配信しないため)。
const APPROVAL_TIMEOUT_MS = 15 * 60 * 1000;

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

  return (
    <div className="space-y-2 border-b border-yellow-500/30 bg-yellow-500/5 px-4 py-2.5">
      {visible.map((a) => {
        const remainMs = APPROVAL_TIMEOUT_MS - (now - (a.receivedAt ?? now));
        return (
          <div key={a.aid} className="flex flex-wrap items-center gap-3">
            <ShieldAlert size={15} className="soft-pulse shrink-0 text-yellow-400" />
            <code className="min-w-0 flex-1 truncate rounded bg-zinc-900 px-2 py-1 font-mono text-[12px] text-yellow-200" title={`cwd: ${a.cwd}`}>
              {a.command}
            </code>
            <span
              className={`shrink-0 tabular-nums text-[11px] ${remainMs < 60_000 ? "font-bold text-red-400" : "text-zinc-500"}`}
              title="この時間を過ぎるとサーバー側で自動的に却下されます"
            >
              自動却下まで {fmtRemain(remainMs)}
            </span>
            <div className="flex gap-1.5">
              <button
                onClick={() => respond(a, true)}
                disabled={busy[a.aid]}
                className="flex items-center gap-1 rounded-md bg-green-600 px-3 py-1 text-[12px] font-semibold text-white hover:bg-green-500 disabled:opacity-50"
              >
                <Check size={12} /> 承認
              </button>
              <button
                onClick={() => respond(a, false)}
                disabled={busy[a.aid]}
                className="flex items-center gap-1 rounded-md border border-red-500/70 px-3 py-1 text-[12px] font-semibold text-red-400 hover:bg-red-500/10 disabled:opacity-50"
              >
                <X size={12} /> 却下
              </button>
            </div>
            {rowError[a.aid] && (
              <p className="w-full text-[11px] font-semibold text-red-400">{rowError[a.aid]}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
