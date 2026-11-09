import type { ChangeAction, ChangeActionResult, ChangePatch, ChangesSummary, ExecutionMode, FolderListing, GitChangeScope, Grants, ReviewSummary, SandboxStatus, SessionSummary, Snapshot } from "./types";

export class ApiError extends Error {
  constructor(message: string, public status: number, public latest?: Grants) { super(message); }
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(String(detail.detail?.message ?? detail.detail ?? response.statusText), response.status, detail.detail?.latest);
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
  health: () => json<{ ok: boolean }>("/api/v1/health", { cache: "no-store" }),
  preferences: () => json<{
    interface_language: string;
    supported: Array<{ id: string; label: string }>;
    note: string;
    theme: "dark" | "light";
    inspector_open: boolean;
    inspector_width: number | null;
  }>("/api/v1/preferences"),
  setPreference: (body: { interface_language?: string; theme?: "dark" | "light"; inspector_open?: boolean; inspector_width?: number }) => json<{
    interface_language: string;
    theme: "dark" | "light";
    inspector_open: boolean;
    inspector_width: number | null;
  }>("/api/v1/preferences", { method: "PUT", body: JSON.stringify(body) }),
  project: () => json<Record<string, unknown>>("/api/v1/project"),
  sessions: (allProjects = false) => json<SessionSummary[]>(`/api/v1/sessions${allProjects ? "?all_projects=true" : ""}`),
  snapshot: (id: string) => json<Snapshot>(`/api/v1/sessions/${id}/snapshot`),
  create: (body: Record<string, unknown>) => json<Snapshot & { submit_error?: string }>("/api/v1/sessions", { method: "POST", body: JSON.stringify(body) }),
  preview: (id: string, projectId?: string) => json<Snapshot>(`/api/v1/sessions/${id}/preview${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  submitTurn: (id: string, body: Record<string, unknown>) => json<Record<string, unknown>>(`/api/v1/sessions/${id}/turns`, { method: "POST", body: JSON.stringify(body) }),
  references: (projectId: string, q: string) => json<{ results: Array<Record<string, unknown>> }>(`/api/v1/workspaces/${projectId}/references?q=${encodeURIComponent(q)}`),
  close: (id: string) => json(`/api/v1/sessions/${id}/close`, { method: "POST", body: "{}" }),
  archive: (id: string, projectId?: string) => json(`/api/v1/sessions/${id}/archive${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, { method: "POST", body: "{}" }),
  relabel: (id: string, label: string, projectId?: string) => json(`/api/v1/sessions/${id}/label${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, { method: "POST", body: JSON.stringify({ label }) }),
  setModel: (id: string, model: string) => json(`/api/v1/sessions/${id}/model`, { method: "POST", body: JSON.stringify({ model }) }),
  commandStatus: (id: string, commandId: string) => json<{ command_id: string; status: string }>(`/api/v1/sessions/${id}/commands/${commandId}`),
  uploadAttachment: async (id: string, file: File) => {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch(`/api/v1/sessions/${id}/attachments`, { method: "POST", credentials: "same-origin", body });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(String(detail.detail?.message ?? detail.detail ?? response.statusText));
    }
    return response.json();
  },
  deleteAttachment: (id: string, attachmentId: string) => json(`/api/v1/sessions/${id}/attachments/${attachmentId}`, { method: "DELETE" }),
  attachmentUrl: (id: string, attachmentId: string) => `/api/v1/sessions/${id}/attachments/${attachmentId}`,
  attachmentThumbnailUrl: (id: string, attachmentId: string) => `/api/v1/sessions/${id}/attachments/${attachmentId}/thumbnail`,
  artifactUrl: (id: string, artifactId: string) => `/api/v1/sessions/${id}/artifacts/${artifactId}`,
  changes: (id: string, scope: GitChangeScope = "working") => json<ChangesSummary>(`/api/v1/sessions/${id}/changes${scope === "working" ? "" : `?scope=${scope}`}`),
  patch: (id: string, path: string, scope: GitChangeScope = "working") => json<ChangePatch>(`/api/v1/sessions/${id}/changes/${path.split("/").map(encodeURIComponent).join("/")}${scope === "working" ? "" : `?scope=${scope}`}`),
  changeAction: (id: string, body: { action: ChangeAction; scope: GitChangeScope; paths: string[]; expected_revision: string; hunk?: number; message?: string; confirm?: boolean }) => json<ChangeActionResult>(`/api/v1/sessions/${id}/changes/actions`, { method: "POST", body: JSON.stringify(body) }),
  workspaces: () => json<{ selected_project_id: string | null; projects: Array<{ project_id: string; name: string; root: string; selected?: boolean; exists?: boolean }> }>("/api/v1/workspaces"),
  folders: (path?: string) => json<FolderListing>(`/api/v1/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  registerWorkspace: (path: string) => json<Record<string, unknown>>("/api/v1/workspaces", { method: "POST", body: JSON.stringify({ path }) }),
  selectWorkspace: (projectId: string) => json<Record<string, unknown>>("/api/v1/workspaces/select", { method: "POST", body: JSON.stringify({ project_id: projectId }) }),
  unregisterWorkspace: (projectId: string) => json<Record<string, unknown>>(`/api/v1/workspaces/${projectId}`, { method: "DELETE" }),
  workspaceTree: (projectId: string, path = "") => json<{ path: string; entries: Array<{ name: string; path: string; kind: string; size: number | null }> }>(`/api/v1/workspaces/${projectId}/tree?path=${encodeURIComponent(path)}`),
  workspaceFile: (projectId: string, path: string) => json<{ path: string; content: string; binary: boolean; line_count?: number }>(`/api/v1/workspaces/${projectId}/file?path=${encodeURIComponent(path)}`),
  workspaceSessions: (projectId: string) => json<SessionSummary[]>(`/api/v1/workspaces/${projectId}/sessions`),
  createInWorkspace: (projectId: string, body: Record<string, unknown>) => json<Snapshot>(`/api/v1/workspaces/${projectId}/sessions`, { method: "POST", body: JSON.stringify(body) }),
  commands: () => json<Array<{ name: string; description: string; usage: string }>>("/api/v1/commands"),
  search: (sessionId: string, q: string, kind: string) => json<{ kind: string; results: Array<Record<string, unknown>> }>(`/api/v1/sessions/${sessionId}/search?q=${encodeURIComponent(q)}&kind=${encodeURIComponent(kind)}`),
  tree: (sessionId: string, path = "") => json<{ path: string; entries: Array<{ name: string; path: string; kind: string; size: number | null }> }>(`/api/v1/sessions/${sessionId}/tree?path=${encodeURIComponent(path)}`),
  file: (sessionId: string, path: string) => json<{ path: string; content: string; binary: boolean; line_count?: number }>(`/api/v1/sessions/${sessionId}/file?path=${encodeURIComponent(path)}`),
  workspaceInfo: (sessionId: string) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/workspace`),
  policy: (sessionId: string, body: { interaction_mode?: string; permission_mode?: string; execution_mode?: ExecutionMode }) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/policy`, { method: "POST", body: JSON.stringify(body) }),
  grants: (sessionId: string) => json<Grants>(`/api/v1/sessions/${sessionId}/grants`),
  revokeGrant: (sessionId: string, ruleId: string, source: string, expected_version: string) => json<Grants>(`/api/v1/sessions/${sessionId}/grants/${ruleId}/revoke`, { method: "POST", body: JSON.stringify({ source, expected_version }) }),
  addGrant: (sessionId: string, resource: Record<string, unknown>, lifetime: string, expected_version: string) => json<Grants>(`/api/v1/sessions/${sessionId}/grants`, { method: "POST", body: JSON.stringify({ resource, lifetime, expected_version }) }),
  sandbox: (sessionId: string, action: "setup" | "cleanup") => json<SandboxStatus>(`/api/v1/sessions/${sessionId}/sandbox`, { method: "POST", body: JSON.stringify({ action }) }),
  review: (sessionId: string) => json<ReviewSummary>(`/api/v1/sessions/${sessionId}/review`),
  reviewPatch: (sessionId: string, path: string) => json<ChangePatch>(`/api/v1/sessions/${sessionId}/review/patch?path=${encodeURIComponent(path)}`),
  reviewAction: (sessionId: string, action: "accept" | "revert", paths: string[], expectedRevision?: string) => json<ChangeActionResult>(`/api/v1/sessions/${sessionId}/review/${action}`, { method: "POST", body: JSON.stringify({ confirm: true, paths, expected_revision: expectedRevision }) }),
  memory: (sessionId: string) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/memory`),
  updateCore: (sessionId: string, body: Record<string, unknown>) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/memory/core`, { method: "PUT", body: JSON.stringify(body) }),
  createSemantic: (sessionId: string, body: Record<string, unknown>) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/memory/semantic`, { method: "POST", body: JSON.stringify(body) }),
  deleteSemantic: (sessionId: string, memoryId: string) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/memory/semantic/${memoryId}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) }),
  deleteEpisode: (sessionId: string, episodeId: string) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/memory/episodes/${episodeId}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) }),
  updateSemantic: (sessionId: string, memoryId: string, body: Record<string, unknown>) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/memory/semantic/${memoryId}`, { method: "PATCH", body: JSON.stringify(body) }),
  workspaceSearch: (projectId: string, q: string, kind: string) => json<{ kind: string; results: Array<Record<string, unknown>> }>(`/api/v1/workspaces/${projectId}/search?q=${encodeURIComponent(q)}&kind=${encodeURIComponent(kind)}`),
  rules: (sessionId: string) => json<{ rules: Array<Record<string, unknown>> }>(`/api/v1/sessions/${sessionId}/rules`),
  saveRule: (sessionId: string, skillId: string, body: Record<string, unknown>) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/rules/${skillId}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteRule: (sessionId: string, skillId: string, scope: string) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/rules/${skillId}`, { method: "DELETE", body: JSON.stringify({ confirm: true, scope }) }),
  schedules: (sessionId: string) => json<Array<Record<string, unknown>>>(`/api/v1/sessions/${sessionId}/schedules`),
  projectSchedules: (projectId: string) => json<Array<Record<string, unknown>>>(`/api/v1/workspaces/${projectId}/schedules`),
  createSchedule: (sessionId: string, body: Record<string, unknown>) => json<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/schedules`, { method: "POST", body: JSON.stringify(body) }),
  scheduleAction: (projectId: string, scheduleId: string, action: string, body: Record<string, unknown> = {}) => json<Record<string, unknown>>(`/api/v1/workspaces/${projectId}/schedules/${scheduleId}/${action}`, { method: "POST", body: JSON.stringify(body) }),
  scheduleRuns: (sessionId: string, scheduleId: string) => json<Array<Record<string, unknown>>>(`/api/v1/sessions/${sessionId}/schedules/${scheduleId}/runs`),
  uploadDocument: async (id: string, file: File) => {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch(`/api/v1/sessions/${id}/documents`, { method: "POST", credentials: "same-origin", body });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(String(detail.detail?.message ?? detail.detail ?? response.statusText));
    }
    return response.json() as Promise<{ id: string; filename: string }>;
  },
};
