import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Star, Inbox, WifiOff, AlertTriangle, Hourglass, X } from "lucide-react";
import Header from "./components/Header.jsx";
import TaskInput from "./components/TaskInput.jsx";
import Sidebar from "./components/Sidebar.jsx";
import RunHeader from "./components/RunHeader.jsx";
import AgentCard from "./components/AgentCard.jsx";
import LogDrawer from "./components/LogDrawer.jsx";
import ArtifactDrawer from "./components/ArtifactDrawer.jsx";
import ChatPanel from "./components/ChatPanel.jsx";
import ApprovalBar from "./components/ApprovalBar.jsx";
import { useRunEvents } from "./useRunEvents.js";
import { deriveAgentState, dependencyOf, segmentByPhase, PHASE_META } from "./derive.js";
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
  // 通知スタック(最大3件)。他Runの完了・失敗と、操作の失敗をここに集約する
  const [notices, setNotices] = useState([]);
  const [plans, setPlans] = useState({});   // run_id -> 起動時の自動判定結果(mode/deliverable/model)
  const [now, setNow] = useState(Date.now());
  const [showDisconnected, setShowDisconnected] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);  // lg未満のオフキャンバス開閉
  const [taskOpen, setTaskOpen] = useState(false);        // 新規タスク入力パネルの開閉
  const [aborting, setAborting] = useState(false);        // 中断要求を出して収まるまで

  const { state, speeds } = useRunEvents(currentId);

  // ---- ポーリング(一覧3s / リソース5s) & 1秒tick(経過時間表示用) ----
  const refreshRuns = useCallback(() => fetchRuns().then((d) => setRuns(d.runs)).catch(() => {}), []);
  useEffect(() => {
    // 初回だけ: Runが1件も無い(=初めて開いた)ときは入力パネルを開いて何をすべきか示す
    fetchRuns()
      .then((d) => {
        setRuns(d.runs);
        if (!d.runs.length) setTaskOpen(true);
      })
      .catch(() => {});
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

  // Runを切り替えたら、前のRunに紐づく画面状態を捨てる
  // (開いたままのログドロワーは別Runのノードを指してしまう)
  useEffect(() => {
    setDrawerNodeId(null);
    setArtifactOpen(false);
  }, [currentId]);

  // 初回ロード時: 直近のライブRun(なければ最新Run)を自動選択。Runが1件も無ければ入力を開く
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
  // 工程(計画→実行→レビュー→統合→回答)ごとのセグメント。時系列は組み替えない
  const segments = useMemo(() => segmentByPhase(agentNodes), [agentNodes]);
  // Run全体の進捗はルート(task)ノードが持つ(code=iter/max, orchestra/swarm=完了サブタスク数)
  const rootProgress = useMemo(() => {
    const root = state.order.map((id) => state.nodes[id]).find((n) => n && n.kind === "task");
    return root?.progress ?? null;
  }, [state.order, state.nodes]);

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

  // ---- 通知(レイアウトシフトさせず右下に積む) ----
  const dismissNotice = useCallback(
    (key) => setNotices((list) => list.filter((n) => n.key !== key)), []);
  const pushNotice = useCallback((n) => {
    setNotices((list) => [...list.filter((x) => x.key !== n.key), n].slice(-3));
    if (n.autoHideMs) setTimeout(() => dismissNotice(n.key), n.autoHideMs);
  }, [dismissNotice]);
  // 操作(中断・削除・再実行)の失敗を黙って捨てず伝える
  const notifyError = useCallback(
    (e) => pushNotice({ key: `err-${Date.now()}`, kind: "error", text: e?.message || "操作に失敗しました", autoHideMs: 8000 }),
    [pushNotice]);

  // 選択していないRunの完了・失敗も拾う(並列3件・放置運用のため通知はポーリング差分で行う。
  // SSEのliveFinishedは選択中Runしか見えない)
  const prevStatusRef = useRef(null);
  useEffect(() => {
    const next = new Map(runs.map((r) => [r.id, r.status]));
    const prev = prevStatusRef.current;
    prevStatusRef.current = next;
    if (!prev) return;   // 初回は既存履歴が一斉に鳴るのでスキップ
    for (const r of runs) {
      const before = prev.get(r.id);
      if (!before || before === r.status) continue;
      if (r.status !== "done" && r.status !== "error") continue;
      const makes = r.mode === "code" || r.mode === "swarm-code";
      pushNotice({
        key: `run-${r.id}-${r.status}`,
        kind: r.status === "error" ? "error" : "done",
        runId: r.id,
        text: r.status === "error"
          ? `エラーで終了: ${(r.task || "").slice(0, 24)}`
          : `${makes ? "成果物が完成" : "完了"}: ${(r.task || "").slice(0, 24)}`,
      });
    }
  }, [runs, pushNotice]);

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

  // ---- アクション ----
  const handleStart = async (payload) => {
    const res = await startRun(payload);
    setPrefill(null);
    setCurrentId(res.run_id);
    setTaskOpen(false);   // 実行を開始したら入力パネルは畳む(作業領域を広く保つ)
    // メインエージェントが決めた進め方・成果物・モデルをRunごとに保持し、Runヘッダーに常設表示する
    setPlans((m) => ({ ...m, [res.run_id]: res }));
    refreshRuns();
  };

  const handleStopAll = async () => {
    const targets = runs.filter((r) => r.status === "running" || r.status === "queued");
    const results = await Promise.allSettled(targets.map((r) => cancelRun(r.id)));
    const ng = results.filter((x) => x.status === "rejected").length;
    if (ng) notifyError(new Error(`${ng}件は停止できませんでした`));
    refreshRuns();
  };

  const handleAbort = () => {
    if (!currentId) return;
    setAborting(true);
    cancelRun(currentId).catch(notifyError).finally(refreshRuns);
  };

  const handleRerun = () => {
    if (!currentRun) return;
    handleStart({
      task: currentRun.task, mode: currentRun.mode, model: currentRun.model,
      critique: currentRun.critique, approve: currentRun.approve, max_iter: currentRun.max_iter,
      deliverable: currentRun.deliverable ?? "auto",
      claude_review: !!currentRun.claude_review,
    }).catch(notifyError);   // Ollama未起動・モデル未導入・RAM不足の理由はここに出る
  };

  const handleEditRerun = () => {
    if (!currentRun) return;
    setPrefill({ ...currentRun });
    setTaskOpen(true);
  };

  // 空のprefillを渡してフォームを既定値へ戻す。null だとTaskInput側のeffectが走らず、
  // 前Runから引き継いだ設定(承認OFF・ClaudeレビューON等)が黙って残る。
  // 既に開いているときは書きかけを消さない。
  const taskOpenRef = useRef(false);
  taskOpenRef.current = taskOpen;
  const handleNewTask = useCallback(() => {
    if (!taskOpenRef.current) setPrefill({});
    setTaskOpen(true);
  }, []);

  const handleDelete = (id) => {
    deleteRun(id)
      .then(() => {
        if (id === currentId) setCurrentId(null);
        refreshRuns();
      })
      .catch(notifyError);
  };

  // 中断は推論ループが降りるまで数秒〜十数秒かかるので、収まったら進行表示を解除する
  useEffect(() => {
    if (!aborting) return;
    if (!currentRun || !["running", "queued"].includes(currentRun.status)) setAborting(false);
  }, [aborting, currentRun]);

  // ---- キーボードショートカット(N=新規タスク / Esc=パネルを閉じる) ----
  useEffect(() => {
    const onKey = (e) => {
      const el = e.target;
      const typing = el instanceof HTMLElement &&
        (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT"
         || el.isContentEditable);
      if (e.key === "Escape") {
        // IME変換中のEscは変換キャンセル。パネルまで閉じてしまわないよう無視する
        if (e.isComposing || e.keyCode === 229) return;
        setSidebarOpen(false);
        if (!drawerNodeId && !artifactOpen) setTaskOpen(false);
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "n" || e.key === "N") {
        e.preventDefault();
        handleNewTask();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [handleNewTask, drawerNodeId, artifactOpen]);

  // ---- 接続・実行状態バナー(排他的に1本だけ表示) ----
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

  const drawerNode = drawerNodeId ? state.nodes[drawerNodeId] : null;

  return (
    // h-dvh: iOS Safari の 100vh はツールバー分を含むため、最下部の入力欄が
    // ツールバーの下に隠れて操作できなくなる。動的ビューポート高で回避する
    <div className="flex h-dvh flex-col overflow-hidden bg-zinc-950 text-zinc-100">
      <Header
        gpu={gpu}
        health={health}
        active={headerActive}
        queueCount={queuedRuns.length}
        onStopAll={handleStopAll}
        stoppable={runs.some((r) => r.status === "running" || r.status === "queued")}
        pendingApprovals={pendingApprovalsTotal}
        onJumpToPending={() => firstPendingRunId && setCurrentId(firstPendingRunId)}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
        sidebarOpen={sidebarOpen}
      />

      <div className="flex min-h-0 flex-1">
        <Sidebar
          runs={runs}
          currentId={currentId}
          onSelect={setCurrentId}
          onDelete={handleDelete}
          onNew={handleNewTask}
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
        />

        {/* 右カラム: 入力 → Runヘッダー → バナー/承認 → ワークスペース → 会話 */}
        <div className="flex min-w-0 flex-1 flex-col">
          {/* 入力パネルは開いている間だけ見せる(state保持のため hidden で隠す) */}
          <div className={taskOpen ? "" : "hidden"}>
            <TaskInput
              models={models}
              onStart={handleStart}
              running={state.running}
              prefill={prefill}
              visible={taskOpen}
              onClose={() => setTaskOpen(false)}
            />
          </div>

          {currentRun && (
            <RunHeader
              run={currentRun}
              plan={plans[currentId]}
              progress={rootProgress}
              running={state.running}
              artifactCount={state.artifacts.length}
              hasAnswer={!!answerNode}
              now={now}
              aborting={aborting}
              onAbort={handleAbort}
              onRerun={handleRerun}
              onEditRerun={handleEditRerun}
              onOpenArtifacts={() => setArtifactOpen(true)}
            />
          )}

          {banner && (
            <div
              className={`flex items-center gap-2 border-b px-4 py-2 text-xs font-semibold ${
                banner.kind === "warn"
                  ? "border-yellow-500/30 bg-yellow-500/10 text-yellow-300"
                  : banner.kind === "error"
                    ? "border-red-500/30 bg-red-500/10 text-red-300"
                    : "border-blue-500/30 bg-blue-500/10 text-blue-300"
              }`}
              role="status"
            >
              <banner.icon size={13} className="shrink-0" />
              {banner.text}
            </div>
          )}

          {currentId && <ApprovalBar runId={currentId} approvals={state.approvals} />}

          {/* ワークスペース: 工程セグメント別のエージェントカード */}
          <main className="min-h-0 flex-1 overflow-y-auto p-4">
            {agentNodes.length === 0 ? (
              <div className="flex h-full min-h-64 flex-col items-center justify-center gap-3 text-zinc-600">
                <Inbox size={36} strokeWidth={1.2} />
                <p className="text-sm">
                  {currentId ? "エージェントの起動を待っています…" : "タスクを実行するとここに進行状況が出ます"}
                </p>
                {!currentId && (
                  <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
                    {SAMPLE_TASKS.map((s) => (
                      <button
                        key={s.text}
                        onClick={() => {
                          setPrefill({ task: s.text });
                          setTaskOpen(true);
                        }}
                        className="rounded-full border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:border-blue-500 hover:text-blue-400"
                      >
                        {s.emoji} {s.text}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              segments.map((seg, i) => {
                const pm = PHASE_META[seg.phase];
                const doneCount = seg.nodes.filter((n) => n.status === "done").length;
                return (
                  <section key={`${seg.phase}-${i}`} className="mb-5 last:mb-0">
                    <div className="mb-2 flex items-center gap-2">
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-zinc-800 text-[10px] font-bold tabular-nums text-zinc-400">
                        {i + 1}
                      </span>
                      <h2 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-300">
                        {pm.label}
                      </h2>
                      <span className="hidden text-[10px] text-zinc-600 sm:inline">{pm.hint}</span>
                      {seg.nodes.length > 1 && (
                        <span className="shrink-0 rounded bg-zinc-800/80 px-1.5 text-[10px] tabular-nums text-zinc-400">
                          {doneCount}/{seg.nodes.length} 完了
                        </span>
                      )}
                      <span className="h-px flex-1 bg-zinc-800" />
                    </div>
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-3">
                      {seg.nodes.map((node) => {
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
                          />
                        );
                      })}
                    </div>
                  </section>
                );
              })
            )}
          </main>

          {/* 会話で成果物を修正する(常設・既定は入力行のみ) */}
          <ChatPanel
            runId={currentId}
            nodes={state.nodes}
            order={state.order}
            running={state.running}
            resumable={state.resumable}
            runStatus={currentRun?.status}
            onSent={refreshRuns}
          />
        </div>
      </div>

      {/* 通知スタック(右下・自動展開はしない。他Runの完了/失敗と操作失敗を積む) */}
      {notices.length > 0 && (
        <div className="fixed bottom-5 right-5 z-30 flex w-80 flex-col gap-2" role="status">
          {notices.map((n) => (
            <div
              key={n.key}
              className={`flex items-center gap-2.5 rounded-xl border bg-zinc-900 px-3 py-2.5 shadow-2xl ${
                n.kind === "error" ? "border-red-500/60" : "border-yellow-500/50"
              }`}
            >
              {n.kind === "error"
                ? <AlertTriangle size={15} className="shrink-0 text-red-400" />
                : <Star size={15} className="shrink-0 text-yellow-400" fill="currentColor" />}
              <span className="min-w-0 flex-1 text-[12.5px] leading-snug text-zinc-100">{n.text}</span>
              {n.runId && (
                <button
                  onClick={() => {
                    if (n.runId === currentId) setArtifactOpen(true);
                    else setCurrentId(n.runId);
                    dismissNotice(n.key);
                  }}
                  className="shrink-0 rounded-md bg-yellow-500 px-2 py-1 text-[11px] font-bold text-zinc-900 hover:bg-yellow-400"
                >
                  表示
                </button>
              )}
              <button
                onClick={() => dismissNotice(n.key)}
                className="shrink-0 rounded p-0.5 text-zinc-500 hover:text-zinc-300"
                title="閉じる"
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
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
