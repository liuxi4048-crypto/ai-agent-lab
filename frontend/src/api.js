// バックエンド(FastAPI)のAPI呼び出し。エンドポイント名・リクエスト構造は server.py と1:1対応。

async function jsonOrThrow(resp) {
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body.detail) detail = body.detail;
    } catch { /* JSONでないエラーボディは無視 */ }
    const err = new Error(detail);
    err.status = resp.status; // 呼び出し側でステータス別分岐(例: 409=解決済み)に使う
    throw err;
  }
  return resp.json();
}

export const fetchRuns = () => fetch("/runs").then(jsonOrThrow);
export const fetchModels = () => fetch("/models").then(jsonOrThrow);
export const fetchHealth = () => fetch("/health").then(jsonOrThrow);
export const fetchGpu = () => fetch("/gpu").then(jsonOrThrow);

export const startRun = (payload) =>
  fetch("/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(jsonOrThrow);

export const cancelRun = (runId) =>
  fetch(`/run/${runId}/cancel`, { method: "POST" }).then(jsonOrThrow);

export const deleteRun = (runId) =>
  fetch(`/run/${runId}`, { method: "DELETE" }).then(jsonOrThrow);

export const continueRun = (runId, message) =>
  fetch(`/run/${runId}/continue`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  }).then(jsonOrThrow);

export const resolveApproval = (runId, aid, approved) =>
  fetch(`/run/${runId}/approvals/${aid}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved }),
  }).then(jsonOrThrow);

// ---- ベンチ(固定プロンプト集による性能測定) ----
export const fetchBenchSuite = () => fetch("/bench/suite").then(jsonOrThrow);

export const startBench = (payload) =>
  fetch("/bench", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(jsonOrThrow);
