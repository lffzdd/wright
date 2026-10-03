import { expect, test } from "@playwright/test";

type Session = {
  session_id: string;
  status: string;
  user_goal: string;
  model: string;
  environment: "local" | "worktree";
  execution_root: string;
  branch_name: string;
  active: boolean;
  recoverable: boolean;
};

const snapshot = (session: Session) => ({
  stream_id: `stream-${session.session_id}`,
  last_seq: 0,
  session,
  history: [],
  timeline: [],
  active_turn: null,
  plan: {},
  pending_interactions: [],
  usage: {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    context_tokens: 0,
    context_limit: 128000,
  },
});

test("isolates sessions and completes a structured permission flow", async ({ page }) => {
  const sessions: Session[] = [];
  const snapshots = new Map<string, ReturnType<typeof snapshot>>();

  await page.addInitScript(() => {
    class MockWebSocket {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSING = 2;
      readonly CLOSED = 3;
      readonly url: string;
      readyState = MockWebSocket.CONNECTING;
      onopen: ((event: Event) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;

      constructor(url: string | URL) {
        this.url = String(url);
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        }, 0);
      }

      send(raw: string) {
        const command = JSON.parse(raw) as { type: string; prompt?: string; request_id?: string };
        const sessionId = this.url.match(/sessions\/([^/]+)\/stream/)?.[1] ?? "unknown";
        const turnId = "turn-e2e";
        const emit = (seq: number, type: string, payload: Record<string, unknown>) => {
          const event = {
            version: 2,
            stream_id: `stream-${sessionId}`,
            event_id: `event-${sessionId}-${seq}`,
            seq,
            emitted_at: new Date().toISOString(),
            project_id: "project-e2e",
            session_id: sessionId,
            turn_id: turnId,
            type,
            payload,
          };
          this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
        };
        if (command.type === "interaction.respond") {
          setTimeout(() => emit(6, "interaction.resolved", {
            request_id: command.request_id ?? "",
            kind: "permission",
          }), 0);
          setTimeout(() => emit(7, "content.final", { content: "Web smoke passed." }), 10);
          setTimeout(() => emit(8, "turn.completed", {}), 20);
          return;
        }
        if (command.type !== "turn.submit") return;
        setTimeout(() => emit(1, "turn.started", { prompt: command.prompt }), 0);
        setTimeout(() => emit(2, "content.delta", { piece: "Web smoke " }), 10);
        setTimeout(() => emit(3, "tool.planned", {
          call_id: "call-permission",
          name: "write_file",
          arguments: { file: "/tmp/outside/result.txt" },
          phase: "planned",
        }), 20);
        setTimeout(() => emit(4, "tool.awaiting_approval", {
          call_id: "call-permission",
          name: "write_file",
          arguments: { file: "/tmp/outside/result.txt" },
          phase: "awaiting_approval",
        }), 25);
        setTimeout(() => emit(5, "interaction.requested", {
          request_id: "transport-request",
          kind: "permission",
          tool_name: "write_file",
          subject: "/tmp/outside/result.txt",
          reason: "Target is outside the current session scope",
          risk_flags: ["path_outside_scope", "writes_files"],
          targets: ["/tmp/outside/result.txt"],
          principal: sessionId,
          choices: [
            { id: "allow_once", label: "Allow once", scope: "This invocation", persistence: "No save" },
            { id: "allow_session_directory_write", label: "Conversation", scope: "/tmp/outside", persistence: "Saved", lifetime: "session", resource_kind: "directory", operations: ["file_read", "file_write"] },
            { id: "allow_project_rule", label: "Project", scope: "/tmp/outside/result.txt", persistence: "Saved", lifetime: "project", resource_kind: "file", operations: ["file_read", "file_write"] },
            { id: "deny", label: "Deny", scope: "No execution", persistence: "No save" },
          ],
        }), 30);
      }

      close() {
        this.readyState = MockWebSocket.CLOSED;
        this.onclose?.(new CloseEvent("close"));
      }
    }
    Object.defineProperty(window, "WebSocket", { value: MockWebSocket });
  });

  await page.route("**/api/v1/project", (route) => route.fulfill({
    json: {
      project_id: "project-e2e",
      name: "wright-e2e",
      project_root: "/tmp/wright-e2e",
      git: true,
      capacity: 4,
      active_count: sessions.length,
      default_environment: "local",
      dirty_checkout: false,
    },
  }));
  await page.route("**/api/v1/sessions", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: sessions });
      return;
    }
    const body = route.request().postDataJSON() as { prompt?: string; environment?: "local" | "worktree" };
    const index = sessions.length + 1;
    const session: Session = {
      session_id: `session-${index}`,
      status: "idle",
      user_goal: body.prompt || `Session ${index}`,
      model: "deterministic-e2e",
      environment: body.environment === "worktree" ? "worktree" : "local",
      execution_root: `/tmp/wright-e2e/session-${index}`,
      branch_name: `wright/session-${index}`,
      active: true,
      recoverable: true,
    };
    sessions.push(session);
    const value = snapshot(session);
    snapshots.set(session.session_id, value);
    await route.fulfill({ json: value });
  });
  await page.route("**/api/v1/sessions/*/snapshot", async (route) => {
    const id = route.request().url().match(/sessions\/([^/]+)\/snapshot/)?.[1] ?? "";
    await route.fulfill({ json: snapshots.get(id) });
  });
  await page.route("**/api/v1/sessions/*/changes", (route) => route.fulfill({
    json: { local_warning: false, baseline: "HEAD", changes: [] },
  }));
  await page.route("**/api/v1/sessions/*/turns", async (route) => {
    const id = route.request().url().match(/sessions\/([^/]+)\/turns/)?.[1] ?? "";
    const body = route.request().postDataJSON() as { prompt?: string };
    const session = sessions.find((item) => item.session_id === id);
    const saved = snapshots.get(id);
    if (session && body.prompt) session.user_goal = body.prompt;
    if (saved && body.prompt) saved.session.user_goal = body.prompt;
    await route.fulfill({ json: { type: "command.accepted", payload: { duplicate: false } } });
  });

  let permissionVersion = "permission-v1";
  let authorized = true;
  let revokeAttempts = 0;
  const grants = () => ({ effective_policy: [], version: permissionVersion, grants: authorized ? [{
    id: "external-file", source: "project", lifetime: "project", resource_kind: "file",
    target: "/tmp/outside/result.txt", operations: ["file_read", "file_write"], recursive: false,
    tool: "*", version: permissionVersion,
  }] : [], sandbox: { available: true, state: "ready", provider: "Bubblewrap", platform: "linux" } });
  await page.route("**/api/v1/sessions/*/grants", (route) => route.fulfill({ json: grants() }));
  await page.route("**/api/v1/sessions/*/grants/external-file/revoke", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.source).toBe("project");
    expect(body.expected_version).toBe(permissionVersion);
    revokeAttempts++;
    if (revokeAttempts === 1) {
      permissionVersion = "permission-v2";
      await route.fulfill({ status: 409, json: { detail: { message: "Permissions changed", latest: grants() } } });
    } else {
      authorized = false;
      permissionVersion = "permission-v3";
      await route.fulfill({ json: grants() });
    }
  });

  await page.goto("/");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "This checkout" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("One active session max")).toHaveCount(0);
  await page.getByLabel("Message Wright").fill("First isolated task");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "First isolated task" })).toBeVisible();
  expect(sessions[0]?.environment).toBe("local");

  await page.getByRole("button", { name: /New session \((?:⌘|Ctrl\+)N\)/ }).click();
  await page.getByLabel("Message Wright").fill("Second isolated task");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "Second isolated task" })).toBeVisible();
  await expect(page.getByText("2/4 active")).toBeVisible();
  await expect(page.getByRole("button", { name: /First isolated task/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Second isolated task/ })).toBeVisible();

  await page.getByLabel("Message Wright").fill("Run deterministic smoke");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("button", { name: /Allow once/i })).toBeVisible();
  await page.getByRole("button", { name: "Remember authorization" }).click();
  await expect(page.getByText("/tmp/outside/result.txt · Read, Write · This project long term", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Save and allow" }).click();
  await expect(page.getByText("Web smoke passed.", { exact: true })).toHaveCount(1);
  const permissionsTab = page.getByRole("tab", { name: "Permissions" });
  if (!(await permissionsTab.isVisible())) await page.getByRole("button", { name: /Open inspector \((?:⌘|Ctrl\+)J\)/ }).click();
  await permissionsTab.click();
  await expect(page.getByRole("button", { name: /^Revoke authorization for / })).toBeVisible();
  await page.getByRole("button", { name: /^Revoke authorization for / }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Permissions changed" })).toBeVisible();
  await page.getByRole("button", { name: /^Revoke authorization for / }).click();
  await expect(page.getByRole("button", { name: /^Revoke authorization for / })).toHaveCount(0);
  expect(revokeAttempts).toBe(2);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: /Open inspector \((?:⌘|Ctrl\+)J\)/ }).click();
  await permissionsTab.click();
  await expect(page.getByRole("textbox", { name: "Resource path", exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/permissions-narrow-en.png", fullPage: true });
  await page.route("**/api/v1/preferences", (route) => route.fulfill({ json: { theme: "dark", interface_language: "zh-CN", inspector_open: true } }));
  await page.reload();
  await page.getByRole("button", { name: /打开检查器（(?:⌘|Ctrl\+)J）/ }).click();
  await page.getByRole("tab", { name: "权限", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "资源路径", exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/permissions-narrow-zh.png", fullPage: true });
});

test("switching sessions only changes the view subscription", async ({ page }) => {
  const sessions: Session[] = [];
  const snapshots = new Map<string, ReturnType<typeof snapshot>>();

  await page.addInitScript(() => {
    class MockWebSocket {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;
      readonly url: string;
      readyState = MockWebSocket.CONNECTING;
      onopen: ((event: Event) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;

      constructor(url: string | URL) {
        this.url = String(url);
        const sent = ((window as unknown as { __sent?: string[] }).__sent ??= []);
        setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        }, 0);
        (window as unknown as { __sockets?: MockWebSocket[] }).__sockets ??= [];
        (window as unknown as { __sockets: MockWebSocket[] }).__sockets.push(this);
        void sent;
      }

      send(raw: string) {
        const sent = ((window as unknown as { __sent?: string[] }).__sent ??= []);
        sent.push(raw);
      }

      close() {
        this.readyState = MockWebSocket.CLOSED;
        this.onclose?.(new CloseEvent("close"));
      }
    }
    Object.defineProperty(window, "WebSocket", { value: MockWebSocket });
  });

  await page.route("**/api/v1/project", (route) => route.fulfill({
    json: {
      project_id: "project-e2e",
      name: "wright-e2e",
      project_root: "/tmp/wright-e2e",
      git: true,
      capacity: 4,
      active_count: sessions.length,
      default_environment: "local",
      dirty_checkout: false,
    },
  }));
  await page.route("**/api/v1/sessions", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: sessions });
      return;
    }
    const body = route.request().postDataJSON() as { prompt?: string; environment?: "local" | "worktree" };
    const index = sessions.length + 1;
    const session: Session = {
      session_id: `session-${index}`,
      status: index === 1 ? "running" : "idle",
      user_goal: body.prompt || `Session ${index}`,
      model: "deterministic-e2e",
      environment: body.environment === "worktree" ? "worktree" : "local",
      execution_root: "/tmp/wright-e2e",
      branch_name: `wright/session-${index}`,
      active: true,
      recoverable: true,
    };
    sessions.push(session);
    const value = snapshot(session);
    if (index === 1) {
      value.history = [{
        turn_id: "kept",
        user: "keep going",
        assistant: "still running",
        status: "completed",
      }] as unknown as typeof value.history;
    }
    snapshots.set(session.session_id, value);
    await route.fulfill({ json: value });
  });
  await page.route("**/api/v1/sessions/*/snapshot", async (route) => {
    const id = route.request().url().match(/sessions\/([^/]+)\/snapshot/)?.[1] ?? "";
    await route.fulfill({ json: snapshots.get(id) });
  });
  await page.route("**/api/v1/sessions/*/changes", (route) => route.fulfill({
    json: { local_warning: false, baseline: "HEAD", changes: [] },
  }));
  await page.route("**/api/v1/sessions/*/close", (route) => route.fulfill({
    json: { session_id: "closed", lifecycle: "closed", status: "closed", active: false },
  }));
  await page.route("**/api/v1/sessions/*/turns", async (route) => {
    const id = route.request().url().match(/sessions\/([^/]+)\/turns/)?.[1] ?? "";
    const body = route.request().postDataJSON() as { prompt?: string };
    const session = sessions.find((item) => item.session_id === id);
    const saved = snapshots.get(id);
    if (session && body.prompt) session.user_goal = body.prompt;
    if (saved && body.prompt) saved.session.user_goal = body.prompt;
    await route.fulfill({ json: { type: "command.accepted", payload: { duplicate: false } } });
  });

  await page.goto("/");
  await page.getByLabel("Message Wright").fill("Alpha task");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("still running", { exact: true })).toBeVisible();
  await page.getByLabel("Message Wright").fill("draft for alpha");

  await page.getByRole("button", { name: /New session \((?:⌘|Ctrl\+)N\)/ }).click();
  await page.getByLabel("Message Wright").fill("Beta task");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "Beta task" })).toBeVisible();
  await expect(page.getByText("still running", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Message Wright")).toHaveValue("");

  await page.getByRole("button", { name: /Alpha task/ }).click();
  await expect(page.getByText("still running", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Message Wright")).toHaveValue("draft for alpha");
  const sent = await page.evaluate(() => (window as unknown as { __sent?: string[] }).__sent ?? []);
  expect(sent.join("\n")).not.toContain("turn.cancel");
});
