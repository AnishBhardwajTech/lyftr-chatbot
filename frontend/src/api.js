const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

export function newSessionId() {
  const rand =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
      : Math.random().toString(36).slice(2, 14);
  return `s-${rand}`;
}

// FastAPI 422 bodies carry a list of errors in `detail`; turn anything into one line.
function readableDetail(detail) {
  if (!detail) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

export async function sendChat({ sessionId, message, systemPrompt, simulateFailure }) {
  const body = { session_id: sessionId, message };
  if (systemPrompt && systemPrompt.trim()) body.system_prompt = systemPrompt.trim();

  const headers = { "Content-Type": "application/json" };
  if (simulateFailure) headers["X-Simulate-Failure"] = simulateFailure;

  let res;
  try {
    res = await fetch(`${API_URL}/chat`, { method: "POST", headers, body: JSON.stringify(body) });
  } catch {
    throw new Error("Could not reach the backend. Check your connection and try again.");
  }

  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON body */
  }

  if (!res.ok) {
    const detail = readableDetail(data && data.detail) || res.statusText || "Request failed";
    const prefix =
      res.status === 503
        ? "All LLM providers are unavailable right now"
        : res.status === 422
        ? "Invalid request"
        : `Error ${res.status}`;
    const err = new Error(`${prefix} (${res.status}): ${detail}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export async function fetchHealth() {
  const res = await fetch(`${API_URL}/health`);
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, ...data };
}

export { API_URL };
