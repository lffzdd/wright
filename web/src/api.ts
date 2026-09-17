import type { SessionSummary, Snapshot } from "./types";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(String(detail.detail ?? response.statusText));
  }
  return response.json() as Promise<T>;
}

export async function bootstrap(): Promise<void> {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("bootstrap");
  if (!token) return;
  await json("/api/v1/auth/exchange", { method: "POST", body: JSON.stringify({ token }) });
  history.replaceState(null, "", `${location.pathname}${location.search}`);
}

export const api = {
  project: () => json<Record<string, unknown>>("/api/v1/project"),
  sessions: () => json<SessionSummary[]>("/api/v1/sessions"),
  snapshot: (id: string) => json<Snapshot>(`/api/v1/sessions/${id}/snapshot`),
  create: (body: Record<string, unknown>) => json<Snapshot>("/api/v1/sessions", { method: "POST", body: JSON.stringify(body) }),
  close: (id: string) => json(`/api/v1/sessions/${id}/close`, { method: "POST", body: "{}" }),
  archive: (id: string) => json(`/api/v1/sessions/${id}/archive`, { method: "POST", body: "{}" }),
  setModel: (id: string, model: string) => json(`/api/v1/sessions/${id}/model`, { method: "POST", body: JSON.stringify({ model }) }),
  changes: (id: string) => json<{ local_warning: boolean; baseline: string; changes: Array<{ path: string; status: string }> }>(`/api/v1/sessions/${id}/changes`),
  patch: (id: string, path: string) => json<{ path: string; patch: string; truncated: boolean; binary: boolean }>(`/api/v1/sessions/${id}/changes/${path.split("/").map(encodeURIComponent).join("/")}`),
};
