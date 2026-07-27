// バックエンド(FastAPI)のAPI呼び出し。エンドポイント名・リクエスト構造は server.py と1:1対応。

async function jsonOrThrow(resp) {
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body.detail) detail = body.detail;
    } catch { /* JSONでないエラーボディは無視 */ }
    throw new Error(detail);
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
