import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Star, Inbox, Wand2 } from "lucide-react";
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
  const [plan, setPlan] = useState(null);   // 起動時の自動判定結果(mode/deliverable/model)
  const [now, setNow] = useState(Date.now());

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

  // ---- 完了トースト(レイアウトシフトさせず通知のみ) ----
  // liveFinished はライブ実行の run_finished でのみ増える。
  // 過去Runのスナップショット再生では発火しない。
  useEffect(() => {
    if (state.liveFinished > 0 && !state.finishError) setToast(true);
  }, [state.liveFinished, state.finishError]);

  // ---- アクション ----
  const handleStart = async (payload) => {
    const res = await startRun(payload);
    setPrefill(null);
    setCurrentId(res.run_id);
    setToast(false);
    // メインエージェントが決めた進め方・成果物・モデルを短時間だけ知らせる
    setPlan(res);
    setTimeout(() => setPlan(null), 6000);
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
      />
      <TaskInput models={models} onStart={handleStart} running={state.running} prefill={prefill} />
      <RunPicker runs={runs} currentId={currentId} onSelect={setCurrentId} onDelete={handleDelete} />
      {currentId && <ApprovalBar runId={currentId} approvals={state.approvals} />}

      {/* ワークスペース: エージェントカードグリッド */}
      <main className="flex-1 p-4">
        {agentNodes.length === 0 ? (
          <div className="flex h-full min-h-64 flex-col items-center justify-center gap-3 text-zinc-600">
            <Inbox size={36} strokeWidth={1.2} />
            <p className="text-sm">
              {currentId ? "エージェントの起動を待っています…" : "タスクを入力して実行してください"}
            </p>
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

      {/* 自動判定の結果表示(メインエージェントが何を選んだか) */}
      {plan && (
        <div className="fixed bottom-5 left-5 z-30 flex items-center gap-2 rounded-xl border border-blue-500/40 bg-zinc-900 px-4 py-2.5 text-xs shadow-2xl">
          <Wand2 size={14} className="shrink-0 text-blue-400" />
          <span className="text-zinc-300">
            {PLAN_MODE_LABEL[plan.mode] ?? plan.mode}
            {plan.deliverable ? ` / ${PLAN_DLV_LABEL[plan.deliverable] ?? plan.deliverable}` : ""}
            <span className="mx-1.5 text-zinc-600">·</span>
            <b className="text-blue-400">{plan.model_tag ?? plan.model}</b>
            {plan.claude_review ? <b className="ml-1.5 text-orange-400">+🤖Claudeレビュー</b> : null}
            {plan.reason ? <span className="ml-1.5 text-zinc-500">({plan.reason})</span> : null}
          </span>
        </div>
      )}

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

      {/* 完了済みRunから成果物を開くフローティングボタン */}
      {!toast && state.finished && (state.artifacts.length > 0 || answerNode) && (
        <button
          onClick={() => setArtifactOpen(true)}
          className="fixed bottom-5 right-5 z-30 flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-900 px-4 py-2 text-xs text-zinc-300 shadow-xl hover:border-yellow-500 hover:text-yellow-400"
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
