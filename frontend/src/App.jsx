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

// 下書きの開始チップに混ぜるサンプルタスク
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
  // ---- 単一の選択状態(Master-Detail の真実源) ----
  // {kind:"run", id} = そのRunを表示 / {kind:"draft", from} = 新しいタスクの下書き /
  // {kind:"empty"} = 未選択。旧実装は currentId と taskOpen の2状態に割れていて、
  // 「新しいタスク」を押しても旧Runの画面が下に残り続けた(不満1の直接原因)。
  const [view, setView] = useState({ kind: "empty" });
  const currentId = view.kind === "run" ? view.id : null;
  const drafting = view.kind === "draft";
  const [prefill, setPrefill] = useState(null);
  const [drawerNodeId, setDrawerNodeId] = useState(null);
  const [artifactOpen, setArtifactOpen] = useState(false);
  // 通知スタック(最大3件)。他Runの完了・失敗と、操作の失敗をここに集約する
  const [notices, setNotices] = useState([]);
  const [plans, setPlans] = useState({});   // run_id -> 起動時の自動判定結果(mode/deliverable/model)
  const [now, setNow] = useState(Date.now());
  const [showDisconnected, setShowDisconnected] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);  // lg未満のオフキャンバス開閉
  const [aborting, setAborting] = useState(false);        // 中断要求を出して収まるまで
  // 実行開始直後〜/runsポーリング反映までの仮Run行(3秒待たせず一覧・選択へ反映する)。
  // 連続で2件開始しても先発の楽観行が消えないよう配列で持つ
  const [optimisticRuns, setOptimisticRuns] = useState([]);

  const { state, speeds } = useRunEvents(currentId);

  // ---- ポーリング(一覧3s / リソース5s) & 1秒tick(経過時間表示用) ----
  const refreshRuns = useCallback(() => fetchRuns().then((d) => setRuns(d.runs)).catch(() => {}), []);
  useEffect(() => {
    // 初回だけ: Runが1件も無い(=初めて開いた)ときは下書きから始める
    fetchRuns()
      .then((d) => {
        setRuns(d.runs);
        if (!d.runs.length) setView((v) => (v.kind === "empty" ? { kind: "draft", from: null } : v));
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

  // 初回ロード時: 直近のライブRun(なければ最新Run)を自動選択。
  // ユーザーが先に下書きへ入っていたら奪わない(view=empty のときだけ)。
  const didAutoSelect = useRef(false);
  useEffect(() => {
    if (didAutoSelect.current || !runs.length) return;
    didAutoSelect.current = true;
    const live = runs.find((r) => r.status === "running" || r.status === "queued");
    setView((v) => (v.kind === "empty" ? { kind: "run", id: (live ?? runs[0]).id } : v));
  }, [runs]);

  // ---- 派生状態 ----
  // 楽観挿入した仮Run行は、サーバーの一覧に同idが現れた時点で自然に退役する
  const runsView = useMemo(() => {
    const pending = optimisticRuns.filter((o) => !runs.some((r) => r.id === o.id));
    return pending.length ? [...pending, ...runs] : runs;
  }, [runs, optimisticRuns]);
  useEffect(() => {
    setOptimisticRuns((list) => {
      const next = list.filter((o) => !runs.some((r) => r.id === o.id));
      return next.length === list.length ? list : next;
    });
  }, [runs]);
  const runsViewRef = useRef(runsView);
  runsViewRef.current = runsView;

  const currentRun = runsView.find((r) => r.id === currentId);
  const queuedRuns = useMemo(
    () => runsView.filter((r) => r.status === "queued").sort((a, b) => a.created_at - b.created_at),
    [runsView],
  );
  const runningCount = useMemo(
    () => runsView.filter((r) => r.status === "running").length,
    [runsView],
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

  // 選択中RunでGPU推論中のノード(システム状態ポップオーバーで表示)
  const activeNode = agentNodes.find((n) => n.status === "thinking" || n.status === "generating");
  const headerActive = activeNode
    ? { name: activeNode.title.replace(/^[^\p{L}\p{N}]*/u, "").slice(0, 24), tps: speeds[activeNode.id]?.tps ?? 0 }
    : null;

  const answerNode = [...agentNodes].reverse().find((n) => n.kind === "answer" && n.output);

  // 全Run合計の承認待ち数(ヘッダーの点滅バッジ用)と、最初に見つかった対象Run
  const pendingApprovalsTotal = useMemo(
    () => runsView.reduce((sum, r) => sum + (r.pending_approvals || 0), 0),
    [runsView],
  );
  const firstPendingRunId = useMemo(
    () => runsView.find((r) => r.pending_approvals > 0)?.id ?? null,
    [runsView],
  );

  // 下書きの開始チップ: 直近タスク(重複除去で最大3)+サンプルで計3件
  const suggestions = useMemo(() => {
    const seen = new Set();
    const recent = [];
    for (const r of runsView) {
      const t = (r.task || "").trim();
      if (!t || seen.has(t)) continue;
      seen.add(t);
      recent.push({ emoji: "🕘", text: t });
      if (recent.length >= 3) break;
    }
    const fill = SAMPLE_TASKS.filter((s) => !seen.has(s.text)).slice(0, Math.max(0, 3 - recent.length));
    return [...recent, ...fill];
  }, [runsView]);

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
        // 完了は12秒で自動で消す(放置運用で常時3件貼り付くのを防ぐ)。
        // 失敗は対応の起点になるので手動で閉じるまで残す
        autoHideMs: r.status === "error" ? undefined : 12000,
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

  // ---- ビュー遷移 ----
  const selectRun = useCallback((id) => setView({ kind: "run", id }), []);

  // T2: 新しいタスク(サイドバー / Nキー / 狭幅ヘッダーの+ / 空状態チップ / 継続不可Runからの導線)。
  // 下書きへ入った瞬間に currentId が null になり、useRunEvents が状態を clear するため
  // 旧Runのカード・承認・成果物は個別の後始末なしに全て消える。実行中Runは止まらない。
  const draftingRef = useRef(false);
  draftingRef.current = drafting;
  const handleNewTask = useCallback((seedText) => {
    if (!draftingRef.current) setPrefill(seedText ? { task: seedText } : {});
    else if (seedText) setPrefill({ task: seedText });
    setView((v) => (v.kind === "draft" ? v : { kind: "draft", from: v.kind === "run" ? v.id : null }));
    setSidebarOpen(false);
  }, []);

  // T5: 下書きの取消。直前に見ていたRunへ戻す(消えていたら未選択へ)。
  // キャンセル=明示的な破棄なので本文・設定も消す({task:""} は TaskInput 側で
  // 「本文もクリアする」の意味になる)。消さないと、放棄したはずの本文が次の
  // 「新しいタスク」で無警告に蘇り、気づかず実行される事故につながる。
  // Runを見に行くだけの離脱(T6)は破棄ではないため、そちらでは消さない。
  const handleCancelDraft = useCallback(() => {
    setPrefill({ task: "" });
    setView((v) => {
      if (v.kind !== "draft") return v;
      if (v.from && runsViewRef.current.some((r) => r.id === v.from)) {
        return { kind: "run", id: v.from };
      }
      return { kind: "empty" };
    });
  }, []);

  // ---- アクション ----
  // T3: 実行成功で初めて選択が新Runへ移る。失敗時は下書きに留まる(TaskInput側でエラー表示)
  const handleStart = async (payload) => {
    const res = await startRun(payload);
    setPrefill(null);
    // メインエージェントが決めた進め方・成果物・モデルをRunごとに保持し、実行条件に常設表示する
    setPlans((m) => ({ ...m, [res.run_id]: res }));
    // 楽観挿入: 3秒ポーリングを待たず一覧の先頭に出す(/runs に同idが現れたら退役)
    setOptimisticRuns((list) => [{
      id: res.run_id, task: payload.task, status: "queued",
      created_at: Date.now() / 1000, mode: res.mode, deliverable: res.deliverable,
      model: res.model, model_tag: res.model_tag, pending_approvals: 0, tokens: 0,
      max_iter: payload.max_iter, approve: payload.approve, critique: payload.critique,
      claude_review: res.claude_review,
    }, ...list]);
    setView({ kind: "run", id: res.run_id });
    refreshRuns();
  };

  const handleStopAll = async () => {
    const targets = runsView.filter((r) => r.status === "running" || r.status === "queued");
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

  // T7: 設定変更して再実行 → 設定を引き継いだ下書きへ遷移(キャンセルで元のRunに戻れる)
  const handleEditRerun = () => {
    if (!currentRun) return;
    setPrefill({ ...currentRun, _inherit: true });
    setView({ kind: "draft", from: currentRun.id });
  };

  const handleDelete = (id) => {
    deleteRun(id)
      .then(() => {
        setView((v) => {
          if (v.kind === "run" && v.id === id) return { kind: "empty" };
          if (v.kind === "draft" && v.from === id) return { ...v, from: null };
          return v;
        });
        refreshRuns();
      })
      .catch(notifyError);
  };

  // 中断は推論ループが降りるまで数秒〜十数秒かかるので、収まったら進行表示を解除する
  useEffect(() => {
    if (!aborting) return;
    if (!currentRun || !["running", "queued"].includes(currentRun.status)) setAborting(false);
  }, [aborting, currentRun]);

  // ---- キーボードショートカット(N=新規タスク / Esc=下書き取消・パネルを閉じる) ----
  useEffect(() => {
    const onKey = (e) => {
      const el = e.target;
      const typing = el instanceof HTMLElement &&
        (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT"
         || el.isContentEditable);
      if (e.key === "Escape") {
        // IME変換中のEscは変換キャンセル。下書きまで取り消してしまわないよう無視する
        if (e.isComposing || e.keyCode === 229) return;
        setSidebarOpen(false);
        // ドロワーやポップオーバーが開いていればそちらのEscが先(各自のリスナーで閉じる)
        if (!drawerNodeId && !artifactOpen && draftingRef.current) handleCancelDraft();
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
  }, [handleNewTask, handleCancelDraft, drawerNodeId, artifactOpen]);

  // ---- 接続・実行状態バナー(承認要求が無いときだけ、排他的に1本表示) ----
  const banner = useMemo(() => {
    if (showDisconnected) return { kind: "warn", icon: WifiOff, text: "サーバーと切断 — 再接続中…" };
    if (state.finishError) return { kind: "error", icon: AlertTriangle, text: state.finishError };
    if (currentRun?.status === "interrupted") {
      return { kind: "warn", icon: AlertTriangle, text: "このRunはサーバー停止で中断されました" };
    }
    if (currentRun?.status === "queued") {
      const pos = currentRun.queue_pos ?? queuePos;
      const reason = currentRun.queue_reason ? ` — ${currentRun.queue_reason}` : "";
      return { kind: "info", icon: Hourglass, text: `待機中(あと${pos ?? "?"}件で開始)${reason}` };
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
        runningCount={runningCount}
        onStopAll={handleStopAll}
        stoppable={runsView.some((r) => r.status === "running" || r.status === "queued")}
        pendingApprovals={pendingApprovalsTotal}
        onJumpToPending={() => firstPendingRunId && selectRun(firstPendingRunId)}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
        sidebarOpen={sidebarOpen}
        onNewTask={() => handleNewTask()}
      />

      <div className="flex min-h-0 flex-1">
        <Sidebar
          runs={runsView}
          currentId={currentId}
          drafting={drafting}
          onSelect={selectRun}
          onDelete={handleDelete}
          onNew={() => handleNewTask()}
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
        />

        {/* 右ペイン: view で排他分岐(下書き / Run監視 / 未選択) */}
        <div className="flex min-w-0 flex-1 flex-col">
          {/* 下書き(Composer)。常時マウント+hiddenで書きかけを保持しつつ、
              display:none なので閉じている間は Tab 順からも外れる */}
          <div className={drafting ? "min-h-0 flex-1" : "hidden"}>
            <TaskInput
              models={models}
              onStart={handleStart}
              prefill={prefill}
              visible={drafting}
              onCancel={view.from || runsView.length ? handleCancelDraft : null}
              suggestions={suggestions}
            />
          </div>

          {!drafting && currentRun && (
            <>
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

              {/* AttentionSlot: 要対応を1スロットに絞る(承認 > バナー)。無ければ高さ0 */}
              {state.approvals.length > 0 ? (
                <ApprovalBar runId={currentId} approvals={state.approvals} />
              ) : banner ? (
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
              ) : null}

              {/* ワークスペース: 工程セグメント別のエージェントカード */}
              <main className="min-h-0 flex-1 overflow-y-auto p-4">
                {agentNodes.length === 0 ? (
                  <div className="flex h-full min-h-64 flex-col items-center justify-center gap-3 text-zinc-600">
                    <Inbox size={36} strokeWidth={1.2} />
                    <p className="text-sm">エージェントを起動しています…</p>
                  </div>
                ) : (
                  segments.map((seg, i) => {
                    const pm = PHASE_META[seg.phase];
                    const doneCount = seg.nodes.filter((n) => n.status === "done").length;
                    return (
                      <section key={`${seg.phase}-${i}`} className="mb-5 last:mb-0">
                        {/* 見出しは sticky: 長いセグメントをスクロール中も「今どの工程か」を保つ */}
                        <div className="sticky top-0 z-10 -mx-1 mb-2 flex items-center gap-2 bg-zinc-950/95 px-1 py-1 backdrop-blur">
                          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-zinc-800 text-[10px] font-bold tabular-nums text-zinc-400">
                            {i + 1}
                          </span>
                          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-300">
                            {pm.label}
                          </h2>
                          <span className="hidden text-[10px] text-zinc-600 sm:inline">{pm.hint}</span>
                          {/* 決定的進捗(n/m)は待機許容度に効くため1件でも常時出す */}
                          <span className="shrink-0 rounded bg-zinc-800/80 px-1.5 text-[10px] tabular-nums text-zinc-400">
                            {doneCount}/{seg.nodes.length} 完了
                          </span>
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

              {/* 会話で成果物を修正する(既定は1行のドック) */}
              <ChatPanel
                runId={currentId}
                nodes={state.nodes}
                order={state.order}
                running={state.running}
                resumable={state.resumable}
                runStatus={currentRun?.status}
                onSent={refreshRuns}
                onStartDraft={(text) => handleNewTask(text || undefined)}
              />
            </>
          )}

          {!drafting && !currentRun && (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-zinc-600">
              <Inbox size={36} strokeWidth={1.2} />
              <p className="text-sm">左の一覧からタスクを選ぶか、新しいタスクを開始してください</p>
              <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
                {SAMPLE_TASKS.map((s) => (
                  <button
                    key={s.text}
                    onClick={() => handleNewTask(s.text)}
                    className="rounded-full border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:border-blue-500 hover:text-blue-400"
                  >
                    {s.emoji} {s.text}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 通知スタック(右下・自動展開はしない。他Runの完了/失敗と操作失敗を積む) */}
      {notices.length > 0 && (
        <div className="fixed bottom-5 right-5 z-30 flex w-80 flex-col gap-2" role="status">
          {notices.map((n) => (
            <div
              key={n.key}
              className={`flex items-center gap-2.5 rounded-xl border bg-zinc-800 px-3 py-2.5 shadow-2xl ${
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
                    else selectRun(n.runId);
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
