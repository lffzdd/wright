import { expect, test } from "@playwright/test";

type Session = {
  session_id: string;
  status: string;
  user_goal: string;
  model: string;
  environment: "worktree";
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

test("creates two isolated sessions and settles a streamed answer once", async ({ page }) => {
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
        const command = JSON.parse(raw) as { type: string; prompt?: string };
        if (command.type !== "turn.submit") return;
        const sessionId = this.url.match(/sessions\/([^/]+)\/stream/)?.[1] ?? "unknown";
        const turnId = "turn-e2e";
        const emit = (seq: number, type: string, payload: Record<string, unknown>) => {
          const event = {
            version: 1,
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
        setTimeout(() => emit(1, "turn.started", { prompt: command.prompt }), 0);
        setTimeout(() => emit(2, "content.delta", { piece: "Web smoke " }), 10);
        setTimeout(() => emit(3, "content.final", { content: "Web smoke passed." }), 20);
        setTimeout(() => emit(4, "turn.completed", {}), 30);
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
      default_environment: "worktree",
      dirty_checkout: false,
    },
  }));
  await page.route("**/api/v1/sessions", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: sessions });
      return;
    }
    const body = route.request().postDataJSON() as { prompt?: string };
    const index = sessions.length + 1;
    const session: Session = {
      session_id: `session-${index}`,
      status: "idle",
      user_goal: body.prompt || `Session ${index}`,
      model: "deterministic-e2e",
      environment: "worktree",
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

  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "Choose an execution environment" })).toBeVisible();
  await page.getByLabel("First instruction").fill("First isolated task");
  await page.getByRole("button", { name: "Create session" }).click();
  await expect(page.getByRole("heading", { name: "First isolated task" })).toBeVisible();

  await page.getByLabel("New session").click();
  await page.getByLabel("First instruction").fill("Second isolated task");
  await page.getByRole("button", { name: "Create session" }).click();
  await expect(page.getByRole("heading", { name: "Second isolated task" })).toBeVisible();
  await expect(page.getByText("2/4 active")).toBeVisible();
  await expect(page.getByRole("button", { name: /First isolated task/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Second isolated task/ })).toBeVisible();

  await page.getByLabel("Message Wright").fill("Run deterministic smoke");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Web smoke passed.", { exact: true })).toHaveCount(1);
});
