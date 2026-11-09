import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const contract = JSON.parse(readFileSync(new URL("./fixtures/permission-contract.json", import.meta.url), "utf8"));

async function permissionPage(page: Page, kind: "file" | "shell" | "python" | "http" | "search" | "policy", locale = "en", width = 1440, executionMode: "sandbox" | "host" = "sandbox") {
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
  const session = { execution_mode: executionMode, session_id: "session-permission", active: true, recoverable: true, execution: "waiting_for_input", status: "waiting_for_input", user_goal: "Review permission", environment: "local", model: "deterministic", execution_root: "/project", permission_mode: "default", interaction_mode: "agent" };
  const grants = () => ({ execution_mode: executionMode, version: "permissions-current", sandbox: status(), grants: [], effective_policy: contract.effective_policy, permission_mode: "default", interaction_mode: "agent", boundary_codes: ["web.boundary.priority", "web.boundary.shell"] });
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    if (path.endsWith("/preferences")) body = { theme: "dark", interface_language: locale, inspector_open: kind === "policy" };
    else if (path.endsWith("/health")) body = { ok: true };
    else if (path.endsWith("/project")) body = { project_id: "permission-project", name: "Permission review", project_root: "/project", git: true, capacity: 4, active_count: 1, default_environment: "local" };
    else if (path.endsWith("/workspaces")) body = { selected_project_id: "permission-project", projects: [{ project_id: "permission-project", name: "Permission review", root: "/project", exists: true }] };
    else if (path.endsWith("/sessions")) body = [session];
    else if (path.endsWith("/snapshot")) body = { stream_id: "permission-stream", last_seq: 0, session, history: [], timeline: [], pending_interactions: kind === "policy" ? [] : [{ ...contract.prompts[kind], execution_mode: executionMode }], active_turn: null, plan: {}, usage: {}, notices: [] };
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
    const card = page.locator(".approval-card");
    await expect(card.locator(".perm-target")).toHaveText("/external/result.txt");
    await expect(card.locator(".permission-preview pre")).toContainText("A new file");
    await expect(card.getByRole("status")).toContainText(locale === "en" ? "Preview shortened" : "预览已缩短");
    await expect(card.locator(".approval-prompt")).toHaveCount(0);
    await card.getByRole("button", { name: locale === "en" ? "More options" : "更多选项" }).click();
    const project = card.getByRole("radio", { name: locale === "en" ? /Edits to \/external\/result.txt in this project/ : /在本项目中（保存）修改 \/external\/result.txt/ });
    await expect(project).not.toBeChecked();
    await project.check();
    await card.getByRole("button", { name: locale === "en" ? "Save and allow" : "保存并允许" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/permission-file-${locale}-${width}.png`, fullPage: true });
    await card.getByRole("button", { name: locale === "en" ? "Save and allow" : "保存并允许" }).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_project_rule");
  });
}

test("Shell setup is required before approval; network can be once or a sandbox grant", async ({ page }) => {
  await permissionPage(page, "shell");
  const card = page.locator(".approval-card");
  await expect(card.getByRole("button", { name: /Allow once/i })).toBeDisabled();
  await expect(card.getByRole("button", { name: /Deny/ })).toBeEnabled();
  await page.screenshot({ path: "test-results/permission-shell-setup.png", fullPage: true });
  await card.getByRole("button", { name: "Initialize / retry (administrator)" }).click();
  await expect(card.getByRole("button", { name: /Allow once/i })).toBeEnabled();
  expect(await page.evaluate(() => (window as unknown as { decisions: unknown[] }).decisions.length)).toBe(0);
  await card.getByRole("checkbox").check();
  await expect(card.getByRole("button", { name: /Allow sandbox network for this session/ })).toBeEnabled();
  await card.getByRole("button", { name: "More options" }).click();
  await expect(card.getByRole("button", { name: "Save and allow" })).toBeEnabled();
  await card.getByRole("button", { name: /Allow once with network/i }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/permission-shell-network.png", fullPage: true });
  await card.getByRole("button", { name: /Allow once with network/i }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_once_network");
});

test("policy shows localized permission modes on a narrow screen", async ({ page }) => {
  await permissionPage(page, "policy", "zh-CN", 390);
  await page.getByRole("button", { name: /打开检查器/ }).click();
  await page.getByRole("tab", { name: "权限", exact: true }).click();
  await expect(page.getByRole("radiogroup")).toBeVisible();
  await expect(page.locator(".inspector-body").getByText("default", { exact: true })).toHaveCount(0);
  await expect(page.locator(".inspector-body").getByText("agent", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "test-results/permission-policy-narrow-zh.png", fullPage: true });
});

test("Python arbitrary arguments and subprocess capability stay explicit on a narrow Chinese screen", async ({ page }) => {
  await permissionPage(page, "python", "zh-CN", 390);
  const card = page.locator(".approval-card");
  await expect(card).toContainText("包括执行解释器代码和脚本；此程序可以启动子进程。");
  await expect(card).toContainText("保持离线，并受系统沙箱限制。");
  const quick = card.getByRole("button", { name: /允许.*任意参数运行.*本次会话内/ });
  await expect(quick).toBeEnabled();
  await quick.scrollIntoViewIfNeeded();
  expect(await card.evaluate((element) => element.getBoundingClientRect().width <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/permission-python-narrow-zh.png", fullPage: true });
  await quick.click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_session_command_executable");
});

test("HTTP origin is useful by default while exact path explicitly permits query changes", async ({ page }) => {
  await permissionPage(page, "http", "en", 390);
  const card = page.locator(".approval-card");
  await expect(card.getByRole("button", { name: /Allow GET requests to all paths at/ })).toBeVisible();
  await card.getByRole("button", { name: "More options" }).click();
  const exactPath = card.getByRole("radio", { name: /GET requests to the exact path.*any query parameters.*for this session/ });
  await exactPath.check();
  await page.screenshot({ path: "test-results/permission-http-narrow-en.png", fullPage: true });
  await card.getByRole("button", { name: "Save and allow" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_session_network_path");
});

test("named search tool scope displays operations and uses the server choice", async ({ page }) => {
  await permissionPage(page, "search");
  const card = page.locator(".approval-card");
  await card.getByRole("button", { name: "More options" }).click();
  await expect(card).toContainText("Allowed operations: Network read");
  await expect(card.getByRole("radio", { name: /Use web_search for this session/ })).toBeChecked();
  await card.getByRole("button", { name: "Save and allow" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_session_rule");
});

for (const locale of ["en", "zh-CN"]) {
  test(`host Shell remains explicit without sandbox setup in ${locale}`, async ({ page }) => {
    await permissionPage(page, "shell", locale, 390, "host");
    const card = page.locator(".approval-card");
    await expect(card).toContainText(locale === "en" ? "without OS file or network isolation" : "无系统文件或网络隔离");
    await expect(card.getByRole("checkbox")).toHaveCount(0);
    await expect(card.locator(".shell-readiness")).toHaveCount(0);
    const allow = card.getByRole("button", { name: locale === "en" ? /Allow once/i : /允许一次/ });
    await expect(allow).toBeEnabled();
    await page.screenshot({ path: `/tmp/wright-host-${locale}-390.png`, fullPage: true });
    await allow.click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { decisions: Array<{ answer: string }> }).decisions[0]?.answer)).toBe("allow_once");
  });
}
