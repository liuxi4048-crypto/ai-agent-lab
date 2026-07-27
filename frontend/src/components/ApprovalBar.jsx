import { ShieldAlert, Check, X } from "lucide-react";
import { resolveApproval } from "../api.js";

/** run_command 実行前承認のバー。未応答の承認要求を上部に列挙する。 */
export default function ApprovalBar({ runId, approvals }) {
  if (!approvals?.length) return null;
  return (
    <div className="space-y-2 border-b border-yellow-500/30 bg-yellow-500/5 px-4 py-2.5">
      {approvals.map((a) => (
        <div key={a.aid} className="flex flex-wrap items-center gap-3">
          <ShieldAlert size={15} className="soft-pulse shrink-0 text-yellow-400" />
          <code className="min-w-0 flex-1 truncate rounded bg-zinc-900 px-2 py-1 font-mono text-[12px] text-yellow-200" title={`cwd: ${a.cwd}`}>
            {a.command}
          </code>
          <div className="flex gap-1.5">
            <button
              onClick={() => resolveApproval(runId, a.aid, true)}
              className="flex items-center gap-1 rounded-md bg-green-600 px-3 py-1 text-[12px] font-semibold text-white hover:bg-green-500"
            >
              <Check size={12} /> 承認
            </button>
            <button
              onClick={() => resolveApproval(runId, a.aid, false)}
              className="flex items-center gap-1 rounded-md border border-red-500/70 px-3 py-1 text-[12px] font-semibold text-red-400 hover:bg-red-500/10"
            >
              <X size={12} /> 却下
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
