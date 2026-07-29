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

// Stalled判定の閾値(ms)。generating=トークン生成が止まった場合は仕様どおり10秒。
// thinking=プロンプト評価中はトークン0が正常のため、誤警報を避けて長めに取る。
export const STALL_MS_GENERATING = 10_000;
export const STALL_MS_THINKING = 120_000;

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
