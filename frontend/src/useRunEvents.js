// SSE(/events/{run_id})の購読とRun状態の再構成。
// events.py が発行する全イベント型を reducer で畳み込む。トークンストリームの
// 描画負荷を抑えるため、イベントは一旦バッファし120ms間隔でまとめて適用する。
import { useEffect, useReducer, useRef, useState } from "react";
import { STALL_MS_GENERATING, STALL_MS_THINKING } from "./derive.js";

// think(推論)テキストのクライアント側保持上限(文字)。バックエンド(events.py THINK_CAP)と
// 同じ考え方で無制限成長を防ぐ。
const THINK_CAP = 20_000;

export const initialRunState = {
  connected: false,
  running: false,
  runStartedAt: null,
  runStatus: null,     // 保存済みRun再生時のみ(snapshot.run_status)
  mode: null,
  resumable: false,
  nodes: {},           // id -> node snapshot
  order: [],
  artifacts: [],
  approvals: [],       // 未応答のみ
  finished: false,
  finishError: null,
  liveFinished: 0,   // ライブ中に run_finished を受けた回数(スナップショット再生では増えない)
};

function applyEvent(state, ev) {
  switch (ev.type) {
    case "snapshot": {
      const nodes = {};
      for (const n of ev.nodes) nodes[n.id] = n;
      return {
        ...initialRunState,
        connected: true,
        running: ev.running,
        runStartedAt: ev.run_started_at,
        runStatus: ev.run_status ?? null,
        mode: ev.mode ?? null,
        resumable: !!ev.resumable,
        nodes,
        order: ev.nodes.map((n) => n.id),
        artifacts: ev.artifacts || [],
        // receivedAt: バックエンドは受付時刻を送らないため、UI側で観測した時刻を
        // カウントダウン表示の基準に用いる(サーバー側の実際の自動却下時刻とは近似)。
        approvals: (ev.approvals || []).map((a) => ({ receivedAt: Date.now(), ...a })),
        finished: !ev.running,
      };
    }
    case "reset":
      return { ...initialRunState, connected: true, running: true, runStartedAt: ev.run_started_at };
    case "resumed":
      return { ...state, running: true, finished: false, finishError: null };
    case "node_created":
      return {
        ...state,
        nodes: { ...state.nodes, [ev.node.id]: ev.node },
        order: [...state.order, ev.node.id],
      };
    case "status_changed": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      return {
        ...state,
        nodes: {
          ...state.nodes,
          [ev.id]: { ...node, status: ev.status, started_at: ev.started_at, finished_at: ev.finished_at },
        },
      };
    }
    case "prompt":
    case "title": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      const patch = ev.type === "prompt" ? { prompt: ev.prompt } : { title: ev.title };
      return { ...state, nodes: { ...state.nodes, [ev.id]: { ...node, ...patch } } };
    }
    case "token_progress": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      return {
        ...state,
        nodes: {
          ...state.nodes,
          [ev.id]: { ...node, tokens: ev.tokens, preview: ev.preview,
                     status: node.status === "thinking" ? "generating" : node.status,
                     output: appendPreview(node, ev) },
        },
      };
    }
    case "tokens": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      return {
        ...state,
        nodes: { ...state.nodes, [ev.id]: { ...node, tokens: ev.tokens, ctx_fill: ev.ctx_fill } },
      };
    }
    case "think_progress": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      const cur = node.think || "";
      // total はサーバー側の累計長。snapshot に含まれ済みのバッファ分を
      // 再追記しないための冪等ガード(接続直後の二重表示防止)
      if (ev.total != null && cur.length >= ev.total) return state;
      let think = cur + (ev.piece || "");
      if (think.length > THINK_CAP) think = think.slice(-THINK_CAP);
      return { ...state, nodes: { ...state.nodes, [ev.id]: { ...node, think } } };
    }
    case "log_line": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      return {
        ...state,
        nodes: { ...state.nodes, [ev.id]: { ...node, log: [...(node.log || []), ev.line] } },
      };
    }
    case "progress": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      return { ...state, nodes: { ...state.nodes, [ev.id]: { ...node, progress: [ev.done, ev.total] } } };
    }
    case "node_completed": {
      const node = state.nodes[ev.id];
      if (!node) return state;
      return { ...state, nodes: { ...state.nodes, [ev.id]: { ...node, output: ev.output } } };
    }
    case "approval_requested":
      return {
        ...state,
        approvals: [...state.approvals, {
          aid: ev.aid, node_id: ev.node_id, command: ev.command, cwd: ev.cwd, receivedAt: Date.now(),
        }],
      };
    case "approval_resolved":
      return { ...state, approvals: state.approvals.filter((a) => a.aid !== ev.aid) };
    case "artifacts":
      return { ...state, artifacts: ev.artifacts };
    case "run_finished":
      return { ...state, running: false, finished: true, finishError: ev.error ?? null,
               liveFinished: state.liveFinished + 1 };
    default:
      return state;
  }
}

// token_progress は preview(末尾120字)しか運ばないため、outputは差分結合で近似する。
// 完全な出力は node_completed / snapshot で確定する。
function appendPreview(node, ev) {
  const cur = node.output || "";
  const preview = ev.preview || "";
  for (let overlap = Math.min(cur.length, preview.length); overlap > 0; overlap--) {
    if (cur.endsWith(preview.slice(0, overlap))) return cur + preview.slice(overlap);
  }
  return cur + preview;
}

function reducer(state, action) {
  if (action.type === "batch") return action.events.reduce(applyEvent, state);
  if (action.type === "disconnect") return { ...state, connected: false };
  return applyEvent(state, action);
}

/**
 * runId のSSEを購読し、{state, speeds} を返す。
 * speeds: { [nodeId]: { tps, stalled } } — 1秒間隔で更新(t/s計測とStalled検知)。
 */
export function useRunEvents(runId) {
  const [state, dispatch] = useReducer(reducer, initialRunState);
  const [speeds, setSpeeds] = useState({});
  const bufferRef = useRef([]);
  // nodeId -> { samples: [[ms, tokens]...], lastTokenAt: ms }
  const speedRef = useRef({});
  // 1秒ティックから最新の nodes を参照するための ref。
  // deps に state.nodes を入れると、ストリーミング中は120msバッチ適用のたびに
  // interval が再生成されて1秒に一度も発火しなくなる(t/s・停滞検知が死ぬ)。
  const nodesRef = useRef(state.nodes);
  nodesRef.current = state.nodes;

  useEffect(() => {
    dispatch({ type: "batch", events: [] });
    bufferRef.current = [];
    speedRef.current = {};
    setSpeeds({});
    if (!runId) {
      dispatch({ type: "disconnect" });
      return;
    }

    const es = new EventSource(`/events/${runId}`);
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "token_progress") {
        const now = performance.now();
        const rec = (speedRef.current[ev.id] ??= { samples: [], lastTokenAt: now });
        rec.lastTokenAt = now;
        rec.samples.push([now, ev.tokens]);
        while (rec.samples.length > 2 && now - rec.samples[0][0] > 5000) rec.samples.shift();
      }
      if (ev.type === "snapshot" || ev.type === "reset") {
        speedRef.current = {};
      }
      bufferRef.current.push(ev);
    };
    es.onerror = () => dispatch({ type: "disconnect" });

    const flush = setInterval(() => {
      if (bufferRef.current.length) {
        dispatch({ type: "batch", events: bufferRef.current.splice(0) });
      }
    }, 120);

    return () => {
      es.close();
      clearInterval(flush);
    };
  }, [runId]);

  // t/s と Stalled の定期評価(1秒間隔)。nodes は ref 経由で読む(deps を空にして
  // interval をストリーミング中も1秒ごとに確実に発火させる)
  useEffect(() => {
    const tick = setInterval(() => {
      const now = performance.now();
      const next = {};
      const nodes = nodesRef.current;
      for (const id of Object.keys(nodes)) {
        const node = nodes[id];
        if (node.status !== "thinking" && node.status !== "generating") continue;
        const rec = speedRef.current[id];
        let tps = 0;
        if (rec && rec.samples.length >= 2) {
          const [t0, k0] = rec.samples[0];
          const [t1, k1] = rec.samples[rec.samples.length - 1];
          if (t1 > t0) tps = ((k1 - k0) / (t1 - t0)) * 1000;
          // 最終トークンから1.5秒以上経過していたら実効0 t/s
          if (now - rec.lastTokenAt > 1500) tps = 0;
        }
        const sinceMs = rec
          ? now - rec.lastTokenAt
          : node.started_at
            ? Date.now() - node.started_at * 1000
            : 0;
        const threshold = node.status === "generating" ? STALL_MS_GENERATING : STALL_MS_THINKING;
        next[id] = { tps, stalled: sinceMs > threshold, sinceMs };
      }
      setSpeeds(next);
    }, 1000);
    return () => clearInterval(tick);
  }, []);

  return { state, speeds };
}
