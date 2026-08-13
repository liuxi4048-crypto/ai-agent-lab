import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Star, Inbox, Wand2, WifiOff, AlertTriangle, Hourglass } from "lucide-react";
import Header from "./components/Header.jsx";
import TaskInput from "./components/TaskInput.jsx";
import RunPicker from "./components/RunPicker.jsx";
import AgentCard from "./components/AgentCard.jsx";
import LogDrawer from "./components/LogDrawer.jsx";
import ArtifactDrawer from "./components/ArtifactDrawer.jsx";
import ChatPanel from "./components/ChatPanel.jsx";

// 自動判定の結果表示用ラベル(内部名ではなく「何をするか」を見せる)
const PLAN_MODE_LABEL = {
  code: "制作", "swarm-code": "並列制作", orchestra: "調査・考察", critique: "推敲",
};
const PLAN_DLV_LABEL = { html: "HTMLアプリ", exe: "exe", script: "スクリプト" };
import ApprovalBar from "./components/ApprovalBar.jsx";
import { useRunEvents } from "./useRunEvents.js";
import { deriveAgentState, dependencyOf } from "./derive.js";
import { fetchRuns, fetchModels, fetchHealth, fetchGpu, startRun, cancelRun, deleteRun } from "./api.js";

const DEFAULT_TITLE = "ai-agent-lab";

// 空状態で出すサンプルタスク(クリックでタスク入力へ流し込む)
const SAMPLE_TASKS = [
  { emoji: "🎮", text: "ブロック崩しゲームを作って" },
  { emoji: "📊", text: "CSVを集計するCLIツールを作って" },
  { emoji: "🔍", text: "ローカルLLMの最新動向を調査して" },
];

export default function App() {
  const [runs, setRuns] = useState([]);
  const [models, setModels] = useState(null);
  const [health, setHealth] = useState(null);
  const [gpu, setGpu] = useState(null);
  const [currentId, setCurrentId] = useState(null);
  const [prefill, setPrefill] = useState(null);
  const [drawerNodeId, setDrawerNodeId] = useState(null);
  const [artifactOpen, setArtifactOpen] = useState(false);
  const [toast, setToast] = useState(false);
  const [plans, setPlans] = useState({});   // run_id -> 起動時の自動判定結果(mode/deliverable/model)。常設表示用
  const [now, setNow] = useState(Date.now());
  const [showDisconnected, setShowDisconnected] = useState(false);

  const { state, speeds } = useRunEvents(currentId);

  // ---- ポーリング(一覧3s / リソース5s) & 1秒tick(経過時間表示用) ----
  const refreshRuns = useCallback(() => fetchRuns().then((d) => setRuns(d.runs)).catch(() => {}), []);
  useEffect(() => {
    refreshRuns();
    fetchModels().then(setModels).catch(() => {});
    const t1 = setInterval(refreshRuns, 3000);
    const poll = () => {
      fetchGpu().then(setGpu).catch(() => setGpu(null));
      fetchHealth().then(setHealth).catch(() => setHealth(null));
    };
    poll();
    const t2 = setInterval(poll, 5000);
    const t3 = setInterval(() => setNow(Date.now()), 1000);
    return () => [t1, t2, t3].forEach(clearInterval);
  }, [refreshRuns]);

  // 初回ロード時: 直近のライブRun(なければ最新Run)を自動選択
  const didAutoSelect = useRef(false);
  useEffect(() => {
    if (didAutoSelect.current || !runs.length) return;
    didAutoSelect.current = true;
    const live = runs.find((r) => r.status === "running" || r.status === "queued");
    setCurrentId((live ?? runs[0]).id);
  }, [runs]);

  // ---- 派生状態 ----
  const currentRun = runs.find((r) => r.id === currentId);
  const queuedRuns = useMemo(
    () => runs.filter((r) => r.status === "queued").sort((a, b) => a.created_at - b.created_at),
    [runs],
  );
  const agentNodes = useMemo(
    () => state.order.map((id) => state.nodes[id]).filter((n) => n && n.kind !== "task"),
    [state.order, state.nodes],
  );
  const cascadeFailed = agentNodes.some((n) => n.status === "error");
  const stalledIds = useMemo(
    () => new Set(Object.keys(speeds).filter((id) => speeds[id].stalled)),
    [speeds],
  );
  const runQueued = currentRun?.status === "queued";
  const queuePos = runQueued ? queuedRuns.findIndex((r) => r.id === currentId) + 1 : null;
  const ctx = { runQueued, queuePos, cascadeFailed, stalledIds };

  // ヘッダーの Active 表示: 現在GPUで推論中のノード(選択中Run内)
  const activeNode = agentNodes.find((n) => n.status === "thinking" || n.status === "generating");
  const headerActive = activeNode
    ? { name: activeNode.title.replace(/^[^\p{L}\p{N}]*/u, "").slice(0, 24), tps: speeds[activeNode.id]?.tps ?? 0 }
    : null;

  const answerNode = [...agentNodes].reverse().find((n) => n.kind === "answer" && n.output);

  // 全Run合計の承認待ち数(ヘッダーの点滅バッジ用)と、最初に見つかった対象Run
  const pendingApprovalsTotal = useMemo(
    () => runs.reduce((sum, r) => sum + (r.pending_approvals || 0), 0),
    [runs],
  );
  const firstPendingRunId = useMemo(
    () => runs.find((r) => r.pending_approvals > 0)?.id ?? null,
    [runs],
  );

  // ---- 完了トースト(レイアウトシフトさせず通知のみ) ----
  // liveFinished はライブ実行の run_finished でのみ増える。
  // 過去Runのスナップショット再生では発火しない。
  useEffect(() => {
    if (state.liveFinished > 0 && !state.finishError) setToast(true);
  }, [state.liveFinished, state.finishError]);

  // ---- SSE切断バナー: 頻繁なRun切り替えでの一瞬の未接続状態では出さない(1.5秒の猶予) ----
  useEffect(() => {
    if (!currentId || state.connected) {
      setShowDisconnected(false);
      return;
    }
    const t = setTimeout(() => setShowDisconnected(true), 1500);
    return () => clearTimeout(t);
  }, [currentId, state.connected]);

  // ---- 完了時にタブタイトルで通知(Notification APIは許可プロンプトが煩わしいため使わない) ----
  useEffect(() => {
    document.title = DEFAULT_TITLE;
    const onVisible = () => {
      if (!document.hidden) document.title = DEFAULT_TITLE;
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);
  useEffect(() => {
    if (state.liveFinished > 0 && !state.finishError) {
      document.title = "✅ 完了 — AI Agent Lab";
      // タブが既にアクティブなら、次に離脱→復帰するのを待たずに一定時間後に戻す
      if (!document.hidden) {
        const t = setTimeout(() => { document.title = DEFAULT_TITLE; }, 3000);
        return () => clearTimeout(t);
      }
    }
  }, [state.liveFinished, state.finishError]);

  // ---- 接続・実行状態バナー(RunPicker直下・排他的に1本だけ表示) ----
  const banner = useMemo(() => {
    if (showDisconnected) return { kind: "warn", icon: WifiOff, text: "サーバーと切断 — 再接続中…" };
    if (state.finishError) return { kind: "error", icon: AlertTriangle, text: state.finishError };
    if (currentRun?.status === "interrupted") {
      return { kind: "warn", icon: AlertTriangle, text: "このRunはサーバー停止で中断されました" };
    }
    if (currentRun?.status === "queued") {
      const pos = currentRun.queue_pos ?? queuePos;
      return { kind: "info", icon: Hourglass, text: `待機中(あと${pos ?? "?"}件で開始)` };
    }
    return null;
  }, [showDisconnected, state.finishError, currentRun, queuePos]);

  // ---- アクション ----
  const handleStart = async (payload) => {
    const res = await startRun(payload);
    setPrefill(null);
    setCurrentId(res.run_id);
    setToast(false);
    // メインエージェントが決めた進め方・成果物・モデルをRunごとに保持し、常設表示する
    setPlans((m) => ({ ...m, [res.run_id]: res }));
    refreshRuns();
  };

  const handleStopAll = () => {
    runs
      .filter((r) => r.status === "running" || r.status === "queued")
      .forEach((r) => cancelRun(r.id).catch(() => {}));
    setTimeout(refreshRuns, 500);
  };

  const handleAbort = () => {
    if (currentId) cancelRun(currentId).catch(() => {}).finally(refreshRuns);
  };

  const handleRerun = () => {
    if (!currentRun) return;
    handleStart({
      task: currentRun.task, mode: currentRun.mode, model: currentRun.model,
      critique: currentRun.critique, approve: currentRun.approve, max_iter: currentRun.max_iter,
      deliverable: currentRun.deliverable ?? "auto",
      claude_review: !!currentRun.claude_review,
    }).catch(() => {});
  };

  const handleEditRerun = () => {
    if (currentRun) setPrefill({ ...currentRun });
  };

  const handleDelete = (id) => {
    deleteRun(id)
      .then(() => {
        if (id === currentId) setCurrentId(null);
        refreshRuns();
      })
      .catch(() => {});
  };

  const drawerNode = drawerNodeId ? state.nodes[drawerNodeId] : null;

  return (
    <div className="flex min-h-screen flex-col bg-zinc-950 text-zinc-100">
      <Header
        gpu={gpu}
        health={health}
        active={headerActive}
        queueCount={queuedRuns.length}
        onStopAll={handleStopAll}
        stoppable={runs.some((r) => r.status === "running" || r.status === "queued")}
        pendingApprovals={pendingApprovalsTotal}
        onJumpToPending={() => firstPendingRunId && setCurrentId(firstPendingRunId)}
      />
      <TaskInput models={models} onStart={handleStart} running={state.running} prefill={prefill} />
      <RunPicker runs={runs} currentId={currentId} onSelect={setCurrentId} onDelete={handleDelete} />

      {/* 接続・実行状態バナー */}
      {banner && (
        <div
          className={`flex items-center gap-2 border-b px-4 py-2 text-xs font-semibold ${
            banner.kind === "warn"
              ? "border-yellow-500/30 bg-yellow-500/10 text-yellow-300"
              : banner.kind === "error"
                ? "border-red-500/30 bg-red-500/10 text-red-300"
                : "border-blue-500/30 bg-blue-500/10 text-blue-300"
          }`}
        >
          <banner.icon size={13} className="shrink-0" />
          {banner.text}
        </div>
      )}

      {currentId && <ApprovalBar runId={currentId} approvals={state.approvals} />}

      {/* ワークスペース: エージェントカードグリッド */}
      <main className="flex-1 p-4">
        {/* 自動判定の結果(進め方・成果物・モデル)を選択中Runの上部に常設表示 */}
        {currentId && plans[currentId] && (
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-blue-500/30 bg-blue-500/5 px-3 py-1.5 text-xs text-zinc-400">
            <Wand2 size={12} className="shrink-0 text-blue-400" />
            <span>
              {PLAN_MODE_LABEL[plans[currentId].mode] ?? plans[currentId].mode}
              {plans[currentId].deliverable
                ? ` / ${PLAN_DLV_LABEL[plans[currentId].deliverable] ?? plans[currentId].deliverable}`
                : ""}
              <span className="mx-1.5 text-zinc-600">·</span>
              <b className="text-blue-400">{plans[currentId].model_tag ?? plans[currentId].model}</b>
              {plans[currentId].claude_review ? <b className="ml-1.5 text-orange-400">+🤖Claudeレビュー</b> : null}
              {plans[currentId].reason ? <span className="ml-1.5 text-zinc-500">({plans[currentId].reason})</span> : null}
            </span>
          </div>
        )}

        {agentNodes.length === 0 ? (
          <div className="flex h-full min-h-64 flex-col items-center justify-center gap-3 text-zinc-600">
            <Inbox size={36} strokeWidth={1.2} />
            <p className="text-sm">
              {currentId ? "エージェントの起動を待っています…" : "タスクを入力して実行してください"}
            </p>
            {!currentId && (
              <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
                {SAMPLE_TASKS.map((s) => (
                  <button
                    key={s.text}
                    onClick={() => setPrefill({ task: s.text })}
                    className="rounded-full border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:border-blue-500 hover:text-blue-400"
                  >
                    {s.emoji} {s.text}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {agentNodes.map((node) => {
              const agentState = deriveAgentState(node, ctx);
              const dep = dependencyOf(node, state.nodes, state.order);
              return (
                <AgentCard
                  key={node.id}
                  node={node}
                  agentState={agentState}
                  dep={dep}
                  depState={dep ? deriveAgentState(dep, ctx) : null}
                  tps={speeds[node.id]?.tps}
                  sinceMs={speeds[node.id]?.sinceMs}
                  now={now}
                  onOpen={() => setDrawerNodeId(node.id)}
                  onAbort={handleAbort}
                  onRerun={handleRerun}
                  onEditRerun={handleEditRerun}
                />
              );
            })}
          </div>
        )}
      </main>

      {/* 会話で成果物を修正する(常設) */}
      <ChatPanel
        runId={currentId}
        nodes={state.nodes}
        order={state.order}
        running={state.running}
        resumable={state.resumable}
        onSent={refreshRuns}
      />

      {/* 成果物完成トースト(右下・自動展開はしない) */}
      {toast && (
        <div className="fixed bottom-5 right-5 z-30 flex items-center gap-3 rounded-xl border border-yellow-500/50 bg-zinc-900 px-4 py-3 shadow-2xl">
          <Star size={16} className="text-yellow-400" fill="currentColor" />
          <span className="text-sm text-zinc-100">統合成果物が完成しました</span>
          <button
            onClick={() => {
              setArtifactOpen(true);
              setToast(false);
            }}
            className="rounded-md bg-yellow-500 px-3 py-1 text-xs font-bold text-zinc-900 hover:bg-yellow-400"
          >
            表示する
          </button>
          <button onClick={() => setToast(false)} className="text-xs text-zinc-500 hover:text-zinc-300">
            閉じる
          </button>
        </div>
      )}

      {/* 成果物を開くフローティングボタン。実行中でも成果物が既にあれば出す(トーストとは位置をずらして重ならないようにする) */}
      {(state.artifacts.length > 0 || (state.finished && answerNode)) && (
        <button
          onClick={() => setArtifactOpen(true)}
          className={`fixed right-5 z-30 flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-900 px-4 py-2 text-xs text-zinc-300 shadow-xl hover:border-yellow-500 hover:text-yellow-400 ${
            toast ? "bottom-20" : "bottom-5"
          }`}
        >
          <Star size={13} /> 成果物
        </button>
      )}

      <LogDrawer
        open={!!drawerNodeId}
        onClose={() => setDrawerNodeId(null)}
        node={drawerNode}
        agentState={drawerNode ? deriveAgentState(drawerNode, ctx) : null}
      />
      <ArtifactDrawer
        open={artifactOpen}
        onClose={() => setArtifactOpen(false)}
        artifacts={state.artifacts}
        answerNode={answerNode}
        resumable={state.resumable}
      />
    </div>
  );
}
