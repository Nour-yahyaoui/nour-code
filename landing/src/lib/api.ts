/** The subset of `GET /health` this page renders. */
export type Health = {
  status: "ok" | "degraded" | "down";
  app: string;
  version: string;
  uptime_seconds: number;
  ollama_reachable: boolean;
  ollama_url: string;
  ollama_version: string | null;
  model: string;
  model_loaded: boolean;
  model_running: boolean;
  model_expires_at: string | null;
  available_models: string[];
  running_models: string[];
  tools: number;
  skills_loaded: number;
  active_sessions: number;
  detail: string | null;
};

async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    ...init,
    signal,
    headers: { Accept: "application/json", ...(init.headers ?? {}) },
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${response.statusText}`.trim());
  }
  return (await response.json()) as T;
}

/** `GET /health` — proxied to the local FastAPI server. */
export function fetchHealth(signal: AbortSignal): Promise<Health> {
  return request<Health>("/health", {}, signal);
}

/** `POST /ollama/warmup` — load the model now and keep it for `keepAlive`. */
export function warmupModel(keepAlive: string): Promise<{ running: boolean; expires_at: string | null }> {
  const query = new URLSearchParams({ keep_alive: keepAlive });
  return request(`/ollama/warmup?${query.toString()}`, { method: "POST" });
}

/** Human sentence for a failed health probe. */
export function explainHealthError(error: Error): string {
  if (error.message === "timeout") return "The API did not answer in time.";
  if (error.name === "TypeError" || error.message.includes("Failed to fetch")) {
    return "No server on this origin. Start nour-code with `python -m nour_code --port 8000`.";
  }
  return error.message;
}

export function formatExpiry(iso: string | null): string {
  if (!iso) return "no expiry";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const minutes = Math.round((date.getTime() - Date.now()) / 60_000);
  if (minutes <= 0) return "unloading now";
  return `unloads in ${minutes} min`;
}
