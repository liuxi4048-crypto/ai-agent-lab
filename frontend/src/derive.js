// 9ステート導出とカード表示用の派生ロジック。
// バックエンド(events.py)のノードstatus(waiting/thinking/generating/running/done/error/cancelled)
// と Run status(queued/running/done/error/cancelled)を、UI仕様の9状態へ写像する。

export const AGENT_STATES = {
  active:    { label: "Active",    jp: "GPU推論中",   dot: "bg-blue-400",    text: "text-blue-400",    border: "border-blue-500/60" },
  queued:    { label: "Queued",    jp: "GPU空き待ち", dot: "bg-yellow-400",  text: "text-yellow-400",  border: "border-yellow-500/40" },
  waiting:   { label: "Waiting",   jp: "依存待ち",    dot: "bg-zinc-500",    text: "text-zinc-400",    border: "border-zinc-700" },
  tool:      { label: "Tool",      jp: "ツール実行中", dot: "bg-violet-400",  text: "text-violet-400",  border: "border-violet-500/50" },
  completed: { label: "Completed", jp: "完了",        dot: "bg-green-400",   text: "text-green-400",   border: "border-green-500/40" },
  failed:    { label: "Failed",    jp: "エラー終了",  dot: "bg-red-400",     text: "text-red-400",     border: "border-red-500/60" },
  canceled:  { label: "Canceled",  jp: "連鎖中止",    dot: "bg-neutral-500", text: "text-neutral-400", border: "border-zinc-700" },
  aborted:   { label: "Aborted",   jp: "中断済み",    dot: "bg-zinc-400",    text: "text-zinc-300",    border: "border-zinc-600" },
  stalled:   { label: "Stalled",   jp: "応答停止警告", dot: "bg-orange-400",  text: "text-orange-400",  border: "border-orange-500" },
};

export const KIND_LABEL = {
  planner: "計画", subagent: "作業", aggregator: "統合", answer: "回答",
  coder: "コーディング", reviewer: "レビュー", author: "執筆", round: "ラウンド",
  merger: "統合", task: "タスク", claude: "Claudeレビュー",
};

// Run一覧・Runヘッダーで共用する「Run全体の状態」表示メタ。
export const RUN_STATUS = {
  running: { label: "実行中", dot: "bg-blue-400 soft-pulse", text: "text-blue-400", chip: "border-blue-500/50 bg-blue-500/10" },
  queued: { label: "待機中", dot: "bg-yellow-400", text: "text-yellow-400", chip: "border-yellow-500/40 bg-yellow-500/10" },
  done: { label: "完了", dot: "bg-green-400", text: "text-green-400", chip: "border-green-500/40 bg-green-500/10" },
  error: { label: "エラー", dot: "bg-red-400", text: "text-red-400", chip: "border-red-500/50 bg-red-500/10" },
  cancelled: { label: "中断", dot: "bg-zinc-500", text: "text-zinc-400", chip: "border-zinc-700 bg-zinc-800/60" },
  interrupted: { label: "強制終了", dot: "bg-zinc-600", text: "text-zinc-400", chip: "border-zinc-700 bg-zinc-800/60" },
};

// 内部のモード名・成果物形式は出さず、「何をするか」が分かる日本語で見せる。
export const MODE_LABEL = {
  code: "制作", "swarm-code": "並列制作", orchestra: "調査・考察", critique: "推敲",
};
export const DELIVERABLE_LABEL = { html: "HTMLアプリ", exe: "exe", script: "スクリプト" };

// 工程(フェーズ)。ノードのkindから導出し、ワークスペースを段階ごとに区切って
// 「計画→実行→レビュー→統合→回答」の流れが一目で追えるようにする。
export const PHASE_META = {
  plan: { label: "計画", hint: "タスクの分解・方針決め" },
  work: { label: "実行", hint: "実際に作る・書く" },
  review: { label: "レビュー", hint: "成果の批評・検証" },
  merge: { label: "統合", hint: "成果のまとめ" },
  answer: { label: "回答", hint: "最終成果物・レポート" },
};

const KIND_PHASE = {
  planner: "plan",
  subagent: "work", coder: "work", author: "work", round: "work",
  reviewer: "review", claude: "review",
  aggregator: "merge", merger: "merge",
  answer: "answer",
};

export function phaseOf(node) {
  return KIND_PHASE[node.kind] ?? "work";
}

/**
 * ノード列(表示順・taskを除く)を「連続する同一フェーズ」でまとめ、工程セグメントの配列にする。
 *
 * 時系列を組み替えずにフェーズ見出しを差し込む方式にしている。kindで並べ替えると
 * codeモードの「実行→レビュー→修正→再レビュー」のようなループ構造が消えてしまうため。
 * 同種の並列ノード(subagent/サブコーダー)は自然に1セグメントへ入り、横並びで表示される。
 */
export function segmentByPhase(nodes) {
  const segments = [];
  for (const node of nodes) {
    const phase = phaseOf(node);
    const last = segments[segments.length - 1];
    if (last && last.phase === phase) last.nodes.push(node);
    else segments.push({ phase, nodes: [node] });
  }
  return segments;
}

// Stalled判定の閾値(ms)。generating=トークン生成が止まった場合は仕様どおり10秒。
// thinking=プロンプト評価中はトークン0が正常のため、誤警報を避けて長めに取る。
export const STALL_MS_GENERATING = 10_000;
export const STALL_MS_THINKING = 120_000;

// thinking中にトークン0が続く場合、この時間までは「モデル準備中」(ロード/プロンプト評価)
// とみなす。Stalled(応答停止警告)よりずっと短い、正常起動時によく起きる待機。
export const LOADING_MS = 15_000;

/**
 * ノード1件をUIの9状態へ写像する。
 * @param node    events.py の Node.snapshot()
 * @param ctx     { runQueued, queuePos, cascadeFailed, stalledIds }
 */
export function deriveAgentState(node, ctx) {
  switch (node.status) {
    case "waiting":
      return ctx.runQueued ? { key: "queued", pos: ctx.queuePos } : { key: "waiting" };
    case "thinking":
    case "generating":
      return ctx.stalledIds?.has(node.id) ? { key: "stalled" } : { key: "active" };
    case "running":
      // coder のコマンド/ツール実行フェーズ。GPUは解放されている
      return { key: "tool" };
    case "done":
      return { key: "completed" };
    case "error":
      return { key: "failed" };
    case "cancelled":
      // 同一Run内に error ノードがあれば連鎖中止、なければユーザー中断
      return ctx.cascadeFailed ? { key: "canceled" } : { key: "aborted" };
    default:
      return { key: "waiting" };
  }
}

/**
 * thinking状態でトークン0が続いているノードを「モデル準備中」(ロード/プロンプト評価)と判定する。
 * @param node events.py の Node.snapshot()
 * @param now  Date.now() 相当(ms)
 */
export function isModelLoading(node, now) {
  if (node.status !== "thinking" || node.tokens > 0 || node.started_at == null) return false;
  return now - node.started_at * 1000 >= LOADING_MS;
}

// ctx_fill(コンテキスト充填率)の警告レベル。85%超=warn(黄) / 95%超=danger(赤)。
export function ctxFillLevel(ctxFill) {
  if (ctxFill == null) return "none";
  if (ctxFill > 0.95) return "danger";
  if (ctxFill > 0.85) return "warn";
  return "none";
}

// 並列実行されるノード群(orchestraのsubagent / swarm-codeのサブコーダー)。
// これらは互いに依存せず、グループ直前のノード(Planner等)に依存する。
const isParallelKind = (n) =>
  n.kind === "subagent" || (n.kind === "coder" && n.title.includes("サブコーダー"));

/**
 * 依存先ノードの導出。親(非task)があればそれ、なければ同階層で直前に作られたノード。
 * ノードは実行順に生成されるため「直前ノード=前段」がパイプラインの実態と一致する。
 * ただし並列グループ内の兄弟同士は依存扱いにしない(グループ先頭の前のノードが依存先)。
 */
export function dependencyOf(node, nodes, order) {
  if (node.parent_id) {
    const parent = nodes[node.parent_id];
    if (parent && parent.kind !== "task") return parent;
  }
  const siblings = order
    .map((id) => nodes[id])
    .filter((n) => n && n.parent_id === node.parent_id && n.kind !== "task");
  let idx = siblings.findIndex((n) => n.id === node.id);
  if (idx > 0 && isParallelKind(node)) {
    while (idx > 0 && isParallelKind(siblings[idx - 1]) && siblings[idx - 1].kind === node.kind) idx--;
  }
  return idx > 0 ? siblings[idx - 1] : null;
}

export function fmtElapsed(sec) {
  if (sec == null || sec < 0) return "--";
  const s = Math.floor(sec);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** Run一覧用の相対時刻(epoch秒 → 「3分前」等)。日付が変わるものは月/日で出す。 */
export function fmtAgo(epochSec) {
  if (!epochSec) return "";
  const diff = Date.now() / 1000 - epochSec;
  if (diff < 60) return "たった今";
  if (diff < 3600) return `${Math.floor(diff / 60)}分前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}時間前`;
  const d = new Date(epochSec * 1000);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export function fmtGB(bytes) {
  return (bytes / 1024 ** 3).toFixed(1);
}

/** カードに出す最新1行(coderログ優先、なければ生成プレビュー末尾行) */
export function lastLine(node) {
  if (node.log?.length) return node.log[node.log.length - 1];
  const preview = (node.preview || "").trim();
  if (!preview) return "";
  const lines = preview.split("\n").filter((l) => l.trim());
  return lines[lines.length - 1] || "";
}
