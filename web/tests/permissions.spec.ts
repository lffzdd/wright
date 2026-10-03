import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const contract = JSON.parse(readFileSync(new URL("./fixtures/permission-contract.json", import.meta.url), "utf8"));

async function permissionPage(page: Page, kind: "file" | "shell" | "policy", locale = "en", width = 1440) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1100 });
  await page.addInitScript(() => {
    (window as unknown as { decisions: unknown[] }).decisions = [];
    class Socket {
      static OPEN = 1;
      readyState = 0;
      onopen: (() => void) | null = null;
      onclose: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      constructor() { setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 0); }
      send(raw: string) {
        const command = JSON.parse(raw);
        (window as unknown as { decisions: unknown[] }).decisions.push(command);
        setTimeout(() => this.onmessage?.(new MessageEvent("message", { data: JSON.stringify({
          version: 2, stream_id: "permission-stream", session_id: "session-permission", event_id: "accepted", seq: 1,
          type: "command.accepted", payload: { command_id: command.command_id, command: command.type },
        }) })), 0);
      }
      close() { this.readyState = 3; this.onclose?.(); }
    }
    Object.defineProperty(window, "WebSocket", { value: Socket });
  });
  let available = kind !== "shell";
  const status = () => ({ platform: "win32", provider: "Windows restricted token", state: available ? "ready" : "setup_required", available });
  const session = { session_id: "session-permission", active: true, recoverable: true, execution: "waiting_for_input", status: "waiting_for_input", user_goal: "Review permission", environment: "local", model: "deterministic", execution_root: "/project", permission_mode: "default", interaction_mode: "agent" };
  const grants = () => ({ version: "permissions-current", sandbox: status(), grants: [], effective_policy: contract.effective_policy, permission_mode: "default", interaction_mode: "agent", boundary_codes: ["web.boundary.priority", "web.boundary.shell"] });
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    if (path.endsWith("/preferences")) body = { theme: "dark", interface_language: locale, inspector_open: kind === "policy" };
    else if (path.endsWith("/health")) body = { ok: true };
    else if (path.endsWith("/project")) body = { project_id: "permission-project", name: "Permission review", project_root: "/project", git: true, capacity: 4, active_count: 1, default_environment: "local" };
    else if (path.endsWith("/workspaces")) body = { selected_project_id: "permission-project", projects: [{ project_id: "permission-project", name: "Permission review", root: "/project", exists: true }] };
    else if (path.endsWith("/sessions")) body = [session];
    else if (path.endsWith("/snapshot")) body = { stream_id: "permission-stream", last_seq: 0, session, history: [], timeline: [], pending_interactions: kind === "policy" ? [] : [contract.prompts[kind]], active_turn: null, plan: {}, usage: {}, notices: [] };
    else if (path.endsWith("/grants")) body = grants();
    else if (path.endsWith("/sandbox")) { available = true; body = status(); }
    else if (path.endsWith("/schedules")) body = [];
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Review permission" })).toBeVisible();
}

for (const [locale, width] of [["en", 1440], ["zh-CN", 1440], ["zh-CN", 390]] as const) {
  test(`file approval uses the real backend contract in ${locale} at ${width}px`, async ({ page }) => {
    await permissionPage(page, "file", locale, width);
    const card = page.locator(".interaction-card");
    await expect(card.locator(".perm-target")).toHaveText("/external/result.txt");
    await expect(card.locator(".permission-preview pre")).toContainText("A new file");
    await expect(card.getByRole("status")).toContainText(locale === "en" ? "Preview shortened" : "预览已缩短");
    await expect(card.locator(".perm-command")).not.toContainText("$");
    if (locale === "zh-CN") {
      await expect(card).toContainText("目标不在当前会话的访问范围内");
      await expect(card.locator(".scope-value")).toContainText("仅本次调用");
      await expect(card.locator(".perm-grid")).not.toContainText("This invocation only");
      if (width === 390) expect(await card.locator(".scope-value").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    }
    await card.getByRole("button", { name: locale === "en" ? "Remember authorization" : "记住授权" }).click();
    await expect(card.getByRole("combobox").nth(0)).toHaveValue("file:/external/result.txt");
    await expect(card.getByRole("combobox").nth(2)).toHaveValue("project");
    await card.getByRole("button", { name: locale === "en" ? "Save and allow" : "保存并允许" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/permission-file-${locale}-${width}.png`, fullPage: true });
    await card.getByRole("button", { name: locale === "en" ? "Save and allow" : "保存并允许" }).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_project_rule");
  });
}

test("Shell setup is required before approval and network stays an explicit one-time capability", async ({ page }) => {
  await permissionPage(page, "shell");
  const card = page.locator(".interaction-card");
  await expect(card.getByRole("button", { name: /Allow once/i })).toBeDisabled();
  await expect(card.getByRole("button", { name: /Deny/ })).toBeEnabled();
  await expect(card.locator(".risk-level")).toHaveText("Review required");
  await page.screenshot({ path: "test-results/permission-shell-setup.png", fullPage: true });
  await card.getByRole("button", { name: "Initialize / retry (administrator)" }).click();
  await expect(card.getByRole("button", { name: /Allow once/i })).toBeEnabled();
  expect(await page.evaluate(() => (window as unknown as { decisions: unknown[] }).decisions.length)).toBe(0);
  await card.getByRole("button", { name: "Remember authorization" }).click();
  await expect(card.getByText("Advanced: all projects")).toHaveCount(0);
  await card.getByRole("checkbox").check();
  await expect(card.getByRole("button", { name: "Save and allow" })).toBeDisabled();
  await card.getByRole("button", { name: /Allow once with network/i }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/permission-shell-network.png", fullPage: true });
  await card.getByRole("button", { name: /Allow once with network/i }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_once_network");
});

test("policy shows all resource lifetimes and localized modes on a narrow screen", async ({ page }) => {
  await permissionPage(page, "policy", "zh-CN", 390);
  await page.getByRole("button", { name: /打开检查器/ }).click();
  await page.getByRole("tab", { name: "权限", exact: true }).click();
  const rules = page.locator(".policy-rule");
  await expect(rules.filter({ hasText: "/external/session.txt" }).first()).toContainText("本会话，恢复后仍有效");
  await expect(rules.filter({ hasText: "/external/project.txt" }).first()).toContainText("此项目长期");
  await expect(rules.filter({ hasText: "/external/user.txt" }).first()).toContainText("所有项目长期");
  await expect(page.locator(".inspector-body").getByText("default", { exact: true })).toHaveCount(0);
  await expect(page.locator(".inspector-body").getByText("agent", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "test-results/permission-policy-narrow-zh.png", fullPage: true });
});
