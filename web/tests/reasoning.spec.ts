import { expect, test, type WebSocketRoute } from "@playwright/test";

test("reasoning stays ordered and expanded after completion", async ({ page }) => {
  const session = { session_id: "reasoning", user_goal: "Inspect reasoning", status: "idle", active: true, recoverable: true, environment: "local" };
  const snapshot = { stream_id: "stream", last_seq: 0, session, history: [], timeline: [], active_turn: null, plan: {}, pending_interactions: [], usage: {} };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = path.endsWith("/project") ? { project_id: "p", name: "Test", project_root: "/tmp/test" }
      : path.endsWith("/sessions") ? [session]
      : path.endsWith("/snapshot") ? snapshot
      : path.endsWith("/preferences") ? { interface_language: "en" }
      : path.endsWith("/workspaces") ? { projects: [] }
      : {};
    await route.fulfill({ json });
  });
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket("**/stream?**", (ws) => { socket = ws; });
  await page.goto("/");
  await expect.poll(() => Boolean(socket)).toBe(true);
  let seq = 0;
  const emit = (type: string, payload: Record<string, unknown> = {}) => socket!.send(JSON.stringify({
    version: 2, stream_id: "stream", event_id: `e${++seq}`, seq, emitted_at: "now",
    project_id: "p", session_id: "reasoning", turn_id: "turn", type, payload,
  }));
  emit("turn.started", { prompt: "Inspect reasoning" });
  emit("reasoning.delta", { piece: "Before the tool" });
  emit("tool.planned", { call_id: "c", name: "read_file", arguments: { path: "note.md" } });
  emit("tool.finished", { call_id: "c", ok: true });
  emit("reasoning.delta", { piece: "After the tool" });
  await expect(page.locator(".reasoning-card")).toHaveCount(2);
  await page.locator(".reasoning-card").first().getByRole("button").click();
  emit("content.final", { content: "Final answer" });
  emit("turn.completed");
  await expect(page.locator(".reasoning-card")).toHaveCount(2);
  await expect(page.locator(".reasoning-card").first().getByRole("button")).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("Final answer", { exact: true })).toHaveCount(1);
  const blocks = await page.locator(".timeline .reasoning-card, .timeline .tool-card:not(.reasoning-card), .timeline .msg-body").allTextContents();
  expect(blocks.findIndex((text) => text.includes("Before the tool"))).toBeLessThan(blocks.findIndex((text) => text.includes("read_file")));
  expect(blocks.findIndex((text) => text.includes("read_file"))).toBeLessThan(blocks.findIndex((text) => text.includes("After the tool")));
  expect(blocks.findIndex((text) => text.includes("After the tool"))).toBeLessThan(blocks.findIndex((text) => text.includes("Final answer")));
});
