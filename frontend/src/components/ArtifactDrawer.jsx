import { useEffect, useState } from "react";
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

const TEXT_MAX_CHARS = 20000;

// orchestrator._collect_files 経由の成果物には kind/runnable が付くが、
// artifacts.py の save_artifacts (orchestra/critiqueモード) 経由の成果物は
// {name, path} のみで kind が無い。その場合は拡張子からベストエフォートで推定する。
function effectiveKind(a) {
  if (a.kind) return a.kind;
  const lower = (a.name || "").toLowerCase();
  if (lower.endsWith(".html") || lower.endsWith(".htm")) return "html";
  if (lower.endsWith(".exe")) return "exe";
  if (lower.endsWith(".bat") || lower.endsWith(".cmd")) return "bat";
  return a.kind;
}

// 同様に runnable も無い場合があるため、orchestrator.py の分類基準
// (kind in html/exe/bat)に合わせてフォールバック判定する。
function isRunnable(a) {
  if (a.runnable != null) return a.runnable;
  return ["html", "exe", "bat"].includes(effectiveKind(a));
}

/** 展開されたテキスト成果物の中身(コピー/ダウンロード/別タブ導線つき)。 */
function FilePreviewBody({ artifact, entry, loading }) {
  const [copied, setCopied] = useState(false);

  if (loading) {
    return <p className="border-t border-zinc-800 px-3 py-2 text-[12px] text-zinc-500">読み込み中…</p>;
  }
  if (!entry) return null;
  if (entry.error) {
    return (
      <p className="border-t border-zinc-800 px-3 py-2 text-[12px] text-red-400">
        読み込みに失敗しました: {entry.error}
      </p>
    );
  }

  const full = entry.text ?? "";
  const truncated = full.length > TEXT_MAX_CHARS;
  const shown = truncated ? full.slice(0, TEXT_MAX_CHARS) : full;

  return (
    <div className="border-t border-zinc-800">
      <div className="flex items-center gap-1.5 px-3 py-1.5">
        <button
          onClick={() => {
            navigator.clipboard.writeText(full);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
          className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
        >
          {copied ? <Check size={11} className="text-green-400" /> : <Copy size={11} />}
          {copied ? "コピー済み" : "コピー"}
        </button>
        <a
          href={artifact.path}
          download={artifact.name.split("/").pop()}
          className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
        >
          <Download size={11} /> ダウンロード
        </a>
        <a
          href={artifact.path}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
        >
          <ExternalLink size={11} /> 別タブで開く
        </a>
      </div>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words border-t border-zinc-800 bg-black/30 px-3 py-2 font-mono text-[12px] leading-relaxed text-zinc-200">
        {shown}
        {truncated && "\n…(以降省略。別タブで全文を開けます)"}
      </pre>
    </div>
  );
}

/**
 * 最終成果物ペイン(ドロワー)。保存された成果物ファイルと最終回答を表示。
 * codeモード完了Runには追加指示(会話継続)フォームも出す。
 */
export default function ArtifactDrawer({ open, onClose, artifacts, answerNode, resumable }) {
  const [copied, setCopied] = useState(false);

  // HTMLアプリのインラインプレビュー用state
  const [selectedHtmlPath, setSelectedHtmlPath] = useState(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [tall, setTall] = useState(false);

  // テキスト成果物のインライン展開用state(1件ずつ・アコーディオン)
  const [expandedPath, setExpandedPath] = useState(null);
  const [previewCache, setPreviewCache] = useState({});
  const [loadingPath, setLoadingPath] = useState(null);

  const list = artifacts ?? [];
  const runnable = list.filter((a) => isRunnable(a) && a.path);
  const rest = list.filter((a) => !(isRunnable(a) && a.path));

  const htmlArtifacts = list.filter((a) => effectiveKind(a) === "html" && a.path);
  const defaultHtml = htmlArtifacts.find((a) => isRunnable(a)) ?? htmlArtifacts[0];
  const activeHtml = htmlArtifacts.find((a) => a.path === selectedHtmlPath) ?? defaultHtml;

  // path に対するテキスト内容を取得してキャッシュに積む。?t= はブラウザキャッシュ回避用
  // (同じpathでファイルが書き換わっても304/キャッシュヒットで古い内容が返るのを防ぐ)。
  const fetchPreview = async (path) => {
    setLoadingPath(path);
    try {
      const res = await fetch(`${path}?t=${Date.now()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      setPreviewCache((c) => ({ ...c, [path]: { text } }));
    } catch (e) {
      setPreviewCache((c) => ({ ...c, [path]: { error: e.message || "取得に失敗しました" } }));
    } finally {
      setLoadingPath(null);
    }
  };

  const togglePreview = (a) => {
    if (expandedPath === a.path) {
      setExpandedPath(null);
      return;
    }
    setExpandedPath(a.path);
    if (previewCache[a.path]) return; // 取得済みなら再フェッチしない
    fetchPreview(a.path);
  };

  // artifacts配列が更新されたら(会話での追加指示等で同じファイルが書き換わった可能性が
  // あるため)展開済みキャッシュを破棄する。すでに展開中のファイルがあれば古い内容を
  // 表示し続けないよう即座に取り直す。
  useEffect(() => {
    setPreviewCache({});
    if (expandedPath) fetchPreview(expandedPath);
    // artifacts変化時のみ発火させる(expandedPath変化時はtogglePreview側で取得する)。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifacts]);

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
        {htmlArtifacts.length > 0 && (
          <section>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">プレビュー</h3>
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              {htmlArtifacts.length > 1 && (
                <div className="flex flex-wrap gap-1">
                  {htmlArtifacts.map((a) => (
                    <button
                      key={a.path}
                      onClick={() => setSelectedHtmlPath(a.path)}
                      className={`rounded-md border px-2 py-1 text-[11px] ${
                        activeHtml?.path === a.path
                          ? "border-blue-500 text-blue-400"
                          : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
                      }`}
                    >
                      {a.name.split("/").pop()}
                    </button>
                  ))}
                </div>
              )}
              <div className="ml-auto flex items-center gap-1.5">
                <button
                  onClick={() => setReloadTick((t) => t + 1)}
                  className="rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
                >
                  🔄 再読み込み
                </button>
                {activeHtml && (
                  <a
                    href={activeHtml.path}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
                  >
                    ↗ 別タブで開く
                  </a>
                )}
                <button
                  onClick={() => setTall((v) => !v)}
                  className="rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
                >
                  {tall ? "⤢ 戻す" : "⤢ 広げる"}
                </button>
              </div>
            </div>
            {activeHtml && open ? (
              <iframe
                key={`${activeHtml.path}-${reloadTick}`}
                src={activeHtml.path}
                title={activeHtml.name}
                // ローカルのエージェントが生成した自作成果物を同一オリジンの静的配信から
                // 読み込むだけであり、外部/未検証コンテンツは扱わない。多くの生成物が
                // localStorageや相対fetch等を使うため allow-scripts と allow-same-origin
                // を両方許可する(この組み合わせは一般に注意が必要だが、信頼境界は
                // ローカルオーケストレータ自身にあるためここでは許容する)。
                sandbox="allow-scripts allow-same-origin"
                className={`w-full rounded-lg border border-zinc-800 bg-white ${tall ? "h-[70vh]" : "h-[420px]"}`}
              />
            ) : (
              <div
                className={`flex items-center justify-center rounded-lg border border-dashed border-zinc-800 text-xs text-zinc-600 ${
                  tall ? "h-[70vh]" : "h-[420px]"
                }`}
              >
                ドロワーを開くとプレビューを再開します
              </div>
            )}
          </section>
        )}

        {runnable.length > 0 && (
          <section>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-green-500">
              すぐ実行できる成果物
            </h3>
            <ul className="space-y-2">
              {runnable.map((a, i) => {
                const kind = effectiveKind(a);
                const spec = RUN_KIND[kind] ?? RUN_KIND.html;
                const Icon = spec.icon;
                // html はプレビュー欄で・exe はバイナリなのでここではテキスト展開は出さない
                const canPreviewText = kind !== "html" && kind !== "exe";
                return (
                  <li key={i}>
                    <div className="flex items-stretch gap-1.5">
                      <a
                        href={a.path}
                        {...(spec.download
                          ? { download: a.name.split("/").pop() }
                          : { target: "_blank", rel: "noreferrer" })}
                        className="flex flex-1 items-center gap-2.5 rounded-lg border border-green-600 bg-green-950/40 px-3 py-2.5 text-sm font-semibold text-green-300 hover:bg-green-900/40"
                      >
                        <Icon size={16} className="shrink-0" />
                        <span className="min-w-0 flex-1 truncate">{a.name}</span>
                        <span className="shrink-0 text-[11px] font-normal text-zinc-400">{spec.label}</span>
                      </a>
                      {canPreviewText && (
                        <button
                          onClick={() => togglePreview(a)}
                          title="内容を表示"
                          className="shrink-0 rounded-lg border border-zinc-700 px-2.5 text-sm text-zinc-400 hover:border-blue-500 hover:text-blue-400"
                        >
                          {expandedPath === a.path ? "▲" : "▾"}
                        </button>
                      )}
                    </div>
                    {canPreviewText && expandedPath === a.path && (
                      <div className="mt-1 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900/60">
                        <FilePreviewBody artifact={a} entry={previewCache[a.path]} loading={loadingPath === a.path} />
                      </div>
                    )}
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
                    <div className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900/60">
                      <button
                        onClick={() => togglePreview(a)}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] text-blue-400 hover:text-blue-300"
                      >
                        <FileCode2 size={14} className="shrink-0" />
                        <span className="min-w-0 flex-1 truncate">{a.name}</span>
                        <span className="shrink-0 text-zinc-600">{expandedPath === a.path ? "▲" : "▾"}</span>
                      </button>
                      {expandedPath === a.path && (
                        <FilePreviewBody artifact={a} entry={previewCache[a.path]} loading={loadingPath === a.path} />
                      )}
                    </div>
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
