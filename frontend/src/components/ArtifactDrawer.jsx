import { useState } from "react";
import { Star, FileCode2, ExternalLink, Copy, Check, MessageSquarePlus,
         Play, Download } from "lucide-react";
import Drawer from "./Drawer.jsx";
import Markdown from "./md.jsx";

// 「そのまま動かせる成果物」の見せ方。html はその場で開き、exe/bat は保存させる。
const RUN_KIND = {
  html: { icon: Play, label: "ブラウザで開く", download: false },
  exe: { icon: Download, label: "保存して実行", download: true },
  bat: { icon: Download, label: "保存して実行", download: true },
};

/**
 * 最終成果物ペイン(ドロワー)。保存された成果物ファイルと最終回答を表示。
 * codeモード完了Runには追加指示(会話継続)フォームも出す。
 */
export default function ArtifactDrawer({ open, onClose, artifacts, answerNode, resumable }) {
  const [copied, setCopied] = useState(false);

  const list = artifacts ?? [];
  const runnable = list.filter((a) => a.runnable && a.path);
  const rest = list.filter((a) => !(a.runnable && a.path));

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
        {runnable.length > 0 && (
          <section>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-green-500">
              すぐ実行できる成果物
            </h3>
            <ul className="space-y-2">
              {runnable.map((a, i) => {
                const spec = RUN_KIND[a.kind] ?? RUN_KIND.html;
                const Icon = spec.icon;
                return (
                  <li key={i}>
                    <a
                      href={a.path}
                      {...(spec.download
                        ? { download: a.name.split("/").pop() }
                        : { target: "_blank", rel: "noreferrer" })}
                      className="flex items-center gap-2.5 rounded-lg border border-green-600 bg-green-950/40 px-3 py-2.5 text-sm font-semibold text-green-300 hover:bg-green-900/40"
                    >
                      <Icon size={16} className="shrink-0" />
                      <span className="min-w-0 flex-1 truncate">{a.name}</span>
                      <span className="shrink-0 text-[11px] font-normal text-zinc-400">{spec.label}</span>
                    </a>
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {rest.length > 0 && (
          <section>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
              {runnable.length > 0 ? "そのほかのファイル" : "保存されたファイル"} ({rest.length})
            </h3>
            <ul className="space-y-1.5">
              {rest.map((a, i) => (
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
            {answerNode.status === "done" ? (
              <Markdown text={answerNode.output} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-zinc-200" />
            ) : (
              <pre className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-[12.5px] leading-relaxed text-zinc-200">
                {answerNode.output}
              </pre>
            )}
          </section>
        )}

        {!list.length && !answerNode?.output && (
          <p className="text-sm text-zinc-500">成果物はまだありません。</p>
        )}

        {resumable && (
          <p className="flex items-center gap-1.5 border-t border-zinc-800 pt-4 text-[11px] text-zinc-500">
            <MessageSquarePlus size={12} />
            修正したいときは、この画面を閉じて下部の「会話で修正する」から指示を送ってください。
          </p>
        )}
      </div>
    </Drawer>
  );
}
