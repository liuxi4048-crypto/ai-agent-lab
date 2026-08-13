import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownToLine, Copy, Check, Search } from "lucide-react";
import Drawer from "./Drawer.jsx";
import Markdown from "./md.jsx";
import { AGENT_STATES, KIND_LABEL, ctxFillLevel } from "../derive.js";

const CTX_METER_CLASS = {
  none: "border-zinc-700 text-zinc-500",
  warn: "border-yellow-500 text-yellow-400",
  danger: "border-red-500 text-red-400",
};

// ---- ログ行の分類 --------------------------------------------------------
// agent.py / claude_review.py が emit する実際の行フォーマットに合わせた分類。
// 1件の emit() 呼び出しが改行込みの文字列を1エントリとして node.log に積まれる
// ことがある(run_command の stdout/stderr や REPORT 等)ため、描画側で改行分割
// してから行単位に分類する。

const WARN_RE = /⚠|\[警告\]|\[finish拒否\]|\[retry\]|\[中断\]/;
const TOOLCALL_RE = /^(\s*)->(\s+)([A-Za-z0-9_.]+)\(/;

/** 1行(改行分割後)を種別+スタイルに分類する純関数。 */
function classifyLine(line) {
  const trimmed = line.trim();
  if (!trimmed) return { kind: "blank", className: "" };

  // 警告・エラー系マーカー(⚠ 構文エラー、finish拒否、retry、中断)
  if (WARN_RE.test(line)) {
    return {
      kind: "warn",
      className:
        "rounded border-l-2 border-yellow-500/60 bg-yellow-500/10 px-1.5 py-0.5 text-yellow-300",
    };
  }
  // 完了サマリ
  if (trimmed.startsWith("[FINISH]")) {
    return {
      kind: "finish",
      className:
        "rounded border-l-2 border-green-500/60 bg-green-500/10 px-1.5 py-0.5 font-semibold text-green-300",
    };
  }
  // イテレーション区切り・完了区切り(=== iter N/M phase=X === / === 完了 === / === REPORT ===)
  if (/^===.+===$/.test(trimmed)) {
    return {
      kind: "section",
      className: "mt-3 border-t border-zinc-800/80 pt-1.5 font-semibold text-zinc-400",
    };
  }
  // ツール呼び出し(-> name({...}))
  if (TOOLCALL_RE.test(line)) {
    return { kind: "toolcall", className: "text-violet-300" };
  }
  // ツール実行結果: exit=0 は成功、exit=非0 / [timeout] / [実行エラー] は失敗
  if (trimmed.startsWith("exit=0")) {
    return { kind: "result-ok", className: "text-green-400" };
  }
  if (/^exit=\d+/.test(trimmed) || trimmed.includes("[timeout]") || trimmed.includes("[実行エラー]")) {
    return { kind: "result-error", className: "text-red-400" };
  }
  // ツール結果本文(インデント行、stdout/stderr区切り)
  if (/^ {5}\S/.test(line) || /^---.+---$/.test(trimmed)) {
    return { kind: "result", className: "text-zinc-400" };
  }
  // AI発話
  if (trimmed.startsWith("[AI]")) {
    return { kind: "ai", className: "text-blue-300" };
  }
  // 思考
  if (trimmed.startsWith("[思考]")) {
    return { kind: "think", className: "italic text-zinc-500" };
  }
  // メタ情報(モデル/目標)
  if (trimmed.startsWith("[agent]") || trimmed.startsWith("[goal]")) {
    return { kind: "meta", className: "text-[11px] text-zinc-500" };
  }
  return { kind: "plain", className: "text-zinc-300" };
}

const FILTERS = [
  { key: "all", label: "すべて" },
  { key: "tool", label: "ツール" },
  { key: "warn", label: "警告・エラー" },
  { key: "ai", label: "AI発話" },
];

const FILTER_MATCH = {
  tool: (kind) => kind === "toolcall" || kind === "result" || kind === "result-ok" || kind === "result-error",
  warn: (kind) => kind === "warn" || kind === "result-error",
  ai: (kind) => kind === "ai",
};

const LINE_WINDOW = 200;

/** 検索クエリで文字列を分割する(大文字小文字を区別しない)。 */
function splitByQuery(text, query) {
  if (!query) return [{ text, hit: false }];
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  const parts = [];
  let i = 0;
  while (i < text.length) {
    const idx = lower.indexOf(q, i);
    if (idx === -1) {
      parts.push({ text: text.slice(i), hit: false });
      break;
    }
    if (idx > i) parts.push({ text: text.slice(i, idx), hit: false });
    parts.push({ text: text.slice(idx, idx + q.length), hit: true });
    i = idx + q.length;
  }
  return parts;
}

function HighlightedText({ text, query }) {
  if (!query) return text;
  return splitByQuery(text, query).map((p, i) =>
    p.hit ? (
      <mark key={i} className="rounded bg-yellow-500/30 text-yellow-100">
        {p.text}
      </mark>
    ) : (
      <span key={i}>{p.text}</span>
    )
  );
}

/** ログ1行の描画。極力軽い1 <div> (+ ツール呼び出し時のみ内側に <span>)。 */
function LogLine({ kind, className, text, query }) {
  const base = "whitespace-pre-wrap break-all px-1.5 py-px font-mono text-[12px] leading-[1.6]";
  if (kind === "toolcall") {
    const m = text.match(TOOLCALL_RE);
    if (m) {
      const nameStart = m[1].length + 2 /* -> */ + m[2].length;
      const nameEnd = nameStart + m[3].length;
      return (
        <div className={`${base} ${className}`}>
          <HighlightedText text={`${m[1]}→${m[2]}`} query={query} />
          <span className="font-bold text-violet-200">
            <HighlightedText text={text.slice(nameStart, nameEnd)} query={query} />
          </span>
          <HighlightedText text={text.slice(nameEnd)} query={query} />
        </div>
      );
    }
  }
  return (
    <div className={`${base} ${className}`}>
      <HighlightedText text={text} query={query} />
    </div>
  );
}

function CopyButton({ text }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text || "");
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
      className="flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400 hover:border-blue-500 hover:text-blue-400"
    >
      {done ? <Check size={11} className="text-green-400" /> : <Copy size={11} />}
      {done ? "コピー済み" : "コピー"}
    </button>
  );
}

/**
 * 詳細ログのサイドドロワー。完全なストリーミング出力(CoT含む)と実行ログを表示。
 * 追従スクロールはトグル可能(手動で上へスクロールすると自動解除)。
 */
export default function LogDrawer({ open, onClose, node, agentState }) {
  const [follow, setFollow] = useState(true);
  const [showPrompt, setShowPrompt] = useState(false);
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [showAllLines, setShowAllLines] = useState(false);
  const scrollRef = useRef(null);

  const outputLen = node?.output?.length ?? 0;

  // node.log(生イベント配列)を改行分割し、行単位に分類・IDを振る。
  const classifiedLines = useMemo(() => {
    if (!node?.log?.length) return [];
    const out = [];
    let id = 0;
    for (const entry of node.log) {
      for (const line of String(entry).split("\n")) {
        if (!line.trim()) continue;
        out.push({ id: id++, line, ...classifyLine(line) });
      }
    }
    return out;
  }, [node?.log]);

  const filterCounts = useMemo(() => {
    const c = { all: 0, tool: 0, warn: 0, ai: 0 };
    for (const { kind } of classifiedLines) {
      c.all++;
      if (FILTER_MATCH.tool(kind)) c.tool++;
      if (FILTER_MATCH.warn(kind)) c.warn++;
      if (FILTER_MATCH.ai(kind)) c.ai++;
    }
    return c;
  }, [classifiedLines]);

  const kindFiltered = useMemo(() => {
    if (filter === "all") return classifiedLines;
    const match = FILTER_MATCH[filter];
    return classifiedLines.filter(({ kind }) => match(kind));
  }, [classifiedLines, filter]);

  const trimmedQuery = query.trim();
  const searched = useMemo(() => {
    if (!trimmedQuery) return kindFiltered;
    const lower = trimmedQuery.toLowerCase();
    return kindFiltered.filter(({ line }) => line.toLowerCase().includes(lower));
  }, [kindFiltered, trimmedQuery]);

  const canWindow = classifiedLines.length > LINE_WINDOW;
  // 検索中は「見つからない」を防ぐため常時全件を対象にする(ウィンドウは無効化)。
  const visible = !trimmedQuery && !showAllLines && canWindow ? searched.slice(-LINE_WINDOW) : searched;

  // 表示中のノードが切り替わったら、前ノードのフィルタ・検索語・追従状態を引き継がない
  // (別カードを開いたときに「該当行なしでログが空に見える」不具合の修正)。
  useEffect(() => {
    setFilter("all");
    setQuery("");
    setFollow(true);
    setShowAllLines(false);
  }, [node?.id]);

  // ウィンドウ表示(最新LINE_WINDOW行)中はvisible.lengthがLINE_WINDOWで頭打ちになり、
  // 新規行が増えても値が変化しないため追従スクロールが効かなくなる。末尾行のid
  // (classifyLine時に振られる単調増加カウンタ)を依存値にすることで、
  // ウィンドウの中身が入れ替わったこと自体を検知できるようにする。
  const lastLineId = visible.length ? visible[visible.length - 1].id : -1;
  useEffect(() => {
    if (!open || !follow || !scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [open, follow, outputLen, lastLineId]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (!atBottom && follow) setFollow(false);
  };

  if (!node) return null;
  const meta = agentState ? AGENT_STATES[agentState.key] : null;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      wide
      title={
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-semibold text-zinc-100">{node.title}</span>
          <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
            {KIND_LABEL[node.kind] ?? node.kind}
          </span>
          {meta && (
            <span className={`shrink-0 rounded-full bg-zinc-800/80 px-2 py-0.5 text-[11px] font-semibold ${meta.text}`}>
              {meta.label}
            </span>
          )}
        </div>
      }
    >
      <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-zinc-800/70 px-4 py-2">
        {node.prompt && (
          <button
            onClick={() => setShowPrompt((v) => !v)}
            className={`rounded-md border px-2 py-1 text-[11px] ${
              showPrompt ? "border-blue-500 text-blue-400" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            プロンプト{showPrompt ? "を隠す" : "を表示"}
          </button>
        )}
        <CopyButton text={node.output} />
        {node.ctx_fill != null && (
          <span
            className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] tabular-nums ${CTX_METER_CLASS[ctxFillLevel(node.ctx_fill)]}`}
            title="コンテキスト充填率(実測prompt tokens / num_ctx)"
          >
            コンテキスト {Math.round(node.ctx_fill * 100)}%
          </span>
        )}
        <button
          onClick={() => {
            setFollow((v) => !v);
            if (!follow && scrollRef.current)
              scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
          }}
          className={`ml-auto flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] ${
            follow ? "border-blue-500 text-blue-400" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
          }`}
        >
          <ArrowDownToLine size={11} /> 追従{follow ? "ON" : "OFF"}
        </button>
      </div>

      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {showPrompt && node.prompt && (
          <section>
            <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">Prompt</h3>
            <pre className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-[12px] leading-relaxed text-zinc-400">
              {node.prompt}
            </pre>
          </section>
        )}
        {node.log?.length > 0 && (
          <section>
            <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
              実行ログ ({node.log.length})
            </h3>
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              {FILTERS.map((f) => {
                const count = filterCounts[f.key];
                const active = filter === f.key;
                return (
                  <button
                    key={f.key}
                    disabled={count === 0}
                    onClick={() => setFilter(f.key)}
                    className={`rounded-md border px-2 py-1 text-[11px] tabular-nums ${
                      count === 0
                        ? "cursor-not-allowed border-zinc-800 text-zinc-700"
                        : active
                          ? "border-blue-500 text-blue-400"
                          : "border-zinc-700 text-zinc-400 hover:text-zinc-200"
                    }`}
                  >
                    {f.label} {count}
                  </button>
                );
              })}
              <div className="relative">
                <Search size={11} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-zinc-600" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="ログ内を検索"
                  className="w-32 rounded-md border border-zinc-700 bg-zinc-900 py-1 pl-6 pr-2 text-[11px] text-zinc-200 placeholder:text-zinc-600 focus:border-blue-500 focus:outline-none"
                />
              </div>
              {trimmedQuery && (
                <span className="text-[11px] tabular-nums text-zinc-500">{searched.length}件ヒット</span>
              )}
              {canWindow && !trimmedQuery && (
                <button
                  onClick={() => setShowAllLines((v) => !v)}
                  className="ml-auto rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400 hover:text-zinc-200"
                >
                  {showAllLines ? `最新${LINE_WINDOW}行のみ表示` : `全${searched.length}行を表示`}
                </button>
              )}
            </div>
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-2">
              {visible.length === 0 ? (
                <p className="px-1.5 py-1 text-[12px] text-zinc-600">該当する行がありません</p>
              ) : (
                visible.map((row) => (
                  <LogLine key={row.id} kind={row.kind} className={row.className} text={row.line} query={trimmedQuery} />
                ))
              )}
            </div>
          </section>
        )}
        {node.think && (
          <section>
            <details className="rounded-lg border border-zinc-800 bg-zinc-900/60">
              <summary className="flex cursor-pointer select-none items-center gap-1.5 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 hover:text-zinc-300">
                🧠 思考
                <span className="tabular-nums font-normal text-zinc-600">({node.think.length.toLocaleString()}字)</span>
              </summary>
              <pre className="whitespace-pre-wrap border-t border-zinc-800 p-3 font-mono text-[12px] leading-relaxed text-zinc-500">
                {node.think}
              </pre>
            </details>
          </section>
        )}
        <section>
          <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            出力 {node.tokens > 0 && <span className="tabular-nums">({node.tokens.toLocaleString()} tokens)</span>}
          </h3>
          {agentState?.key === "completed" && node.output ? (
            <Markdown text={node.output} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-zinc-200" />
          ) : (
            <pre className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 font-mono text-[12px] leading-relaxed text-zinc-200">
              {node.output || node.preview || "(まだ出力はありません)"}
            </pre>
          )}
        </section>
      </div>
      </div>
    </Drawer>
  );
}
