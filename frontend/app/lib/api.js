const DEFAULT_TIMEOUT_MS = 245000;

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL || "").replace(/\/$/, "");

async function requestJson(path, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      signal: controller.signal,
      headers: {
        Accept: "application/json"
      }
    });
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      const detail =
        typeof payload.detail === "string"
          ? payload.detail
          : `HTTP ${response.status}`;
      throw new Error(detail);
    }

    return payload;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function fetchAnswer(question) {
  const params = new URLSearchParams({
    question_id: `finory-${Date.now()}`,
    question
  });

  return requestJson(`/answer?${params.toString()}`);
}

export async function fetchHealth() {
  return requestJson("/health", { timeoutMs: 10000 });
}
