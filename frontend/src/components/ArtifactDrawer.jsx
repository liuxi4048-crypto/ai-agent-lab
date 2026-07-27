import { useState } from "react";
import { Star, FileCode2, ExternalLink, Copy, Check, MessageSquarePlus, Loader2 } from "lucide-react";
import Drawer from "./Drawer.jsx";
import { continueRun } from "../api.js";

/**
 * 最終成果物ペイン(ドロワー)。保存された成果物ファイルと最終回答を表示。
 * codeモード完了Runには追加指示(会話継続)フォームも出す。
 */
export default function ArtifactDrawer({ open, onClose, runId, artifacts, answerNode, resumable, onContinued }) {
  const [copied, setCopied] = useState(false);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submitContinue = async () => {
    if (!msg.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await continueRun(runId, msg.trim());
      setMsg("");
      onContinued?.();
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      wide
      title={
        <div className="flex items-center gap-2">
          <Star size={15} className="text-yellow-400" fill="currentColor" />
          <span className="text-sm font-semibold text-zinc-100">統合成果物</span>
        </div>
      }
    >
      <div className="space-y-5 p-4">
        {artifacts?.length > 0 && (
          <section>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
              保存されたファイル ({artifacts.length})
            </h3>
            <ul className="space-y-1.5">
              {artifacts.map((a, i) => (
                <li key={i}>
                  {a.path ? (
                    <a
                      href={a.path}
                      target="_blank"
                      rel="noreferrer"
                      className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-[13px] text-blue-400 hover:border-blue-500"
                    >
                      <FileCode2 size={14} className="shrink-0" />
                      <span className="min-w-0 flex-1 truncate">{a.name}</span>
                      <ExternalLink size={12} className="shrink-0 text-zinc-600" />
                    </a>
                  ) : (
                    <span className="flex items-center gap-2 rounded-lg border border-zinc-800 px-3 py-2 text-[13px] text-zinc-500">
                      <FileCode2 size={14} /> {a.name}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        {answerNode?.output && (
          <section>
            <div className="mb-2 flex items-center gap-2">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">最終レポート</h3>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(answerNode.output);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                }}
                className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
              >
                {copied ? <Check size={11} className="text-green-400" /> : <Copy size={11} />}
                {copied ? "コピー済み" : "コピー"}
              </button>
            </div>
            <pre className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-[12.5px] leading-relaxed text-zinc-200">
              {answerNode.output}
            </pre>
          </section>
        )}

        {!artifacts?.length && !answerNode?.output && (
          <p className="text-sm text-zinc-500">成果物はまだありません。</p>
        )}

        {resumable && (
          <section className="border-t border-zinc-800 pt-4">
            <h3 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
              <MessageSquarePlus size={12} /> 追加指示(同じワークスペースで継続)
            </h3>
            <div className="flex gap-2">
              <textarea
                value={msg}
                onChange={(e) => setMsg(e.target.value)}
                rows={2}
                placeholder="成果物の修正・追加機能の指示…"
                className="flex-1 resize-none rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-blue-500"
              />
              <button
                onClick={submitContinue}
                disabled={busy || !msg.trim()}
                className="self-end rounded-md bg-blue-600 px-3 py-2 text-sm font-semibold text-white hover:bg-blue-500 disabled:bg-zinc-700 disabled:text-zinc-500"
              >
                {busy ? <Loader2 size={14} className="animate-spin" /> : "送信"}
              </button>
            </div>
            {error && <p className="mt-1.5 text-xs text-red-400">{error}</p>}
          </section>
        )}
      </div>
    </Drawer>
  );
}
