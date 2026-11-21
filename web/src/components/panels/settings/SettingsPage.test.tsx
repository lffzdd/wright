import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  McpConfigView, ModelOverview, PermissionDefaultsView, RuntimeSetting, RuntimeSettingsView, SecretsView, SkillSwitchesView,
} from "../../../types";
import { SettingsPage } from "../SettingsPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function setting(key: string, patch: Partial<RuntimeSetting> = {}): RuntimeSetting {
  return {
    key, kind: "str", choices: [], minimum: null, description: "", default: "", value: "",
    source: "default", user_value: null, project_value: null, env: [], env_override: false,
    scopes: ["user"], ...patch,
  };
}

const overview: ModelOverview = {
  endpoint: { base_url: "", key_env: "OPENAI_API_KEY", key_set: false, provider: "" },
  providers: [
    { id: "openai", name: "OpenAI", base_url: "https://api.openai.com/v1", key_env: "OPENAI_API_KEY", key_set: false, reachable: false },
    { id: "anthropic", name: "Anthropic", base_url: "https://api.anthropic.com/v1/", key_env: "ANTHROPIC_API_KEY", key_set: true, reachable: true },
  ],
  models: [
    { name: "gpt-4o", provider: "openai", context_window: 128000, preset: false, price: { input: 2.5, output: 10, cache_read: null } },
    { name: "claude-opus-5-5", provider: "anthropic", context_window: 1000000, preset: true, price: { input: 5, output: 25, cache_read: 0.5 } },
  ],
};

function view(patch: Record<string, Partial<RuntimeSetting>> = {}, models: ModelOverview | null = overview): RuntimeSettingsView {
  const base = [
    setting("llm.model", { scopes: ["user", "project"], env: ["OPENAI_MODEL"] }),
    setting("llm.memory_model", { scopes: ["user", "project"] }),
    setting("llm.models", { kind: "list", default: [], value: [] }),
    setting("llm.base_url", { env: ["OPENAI_BASE_URL"] }),
    setting("llm.transport", { kind: "choice", choices: ["auto", "chat", "responses"], default: "auto", value: "auto" }),
    setting("llm.context_limit", { kind: "int", default: 0, value: 0, scopes: ["user", "project"] }),
    setting("llm.prices", { kind: "list", default: [], value: [] }),
    setting("session.auto_title", { kind: "bool", default: true, value: true }),
    setting("memory.enabled", { kind: "bool", default: true, value: true }),
    setting("memory.recall", { kind: "bool", default: true, value: true }),
    setting("memory.auto_extract", { kind: "bool", default: true, value: true }),
    setting("knowledge.enabled", { kind: "bool", default: false, value: false }),
    setting("knowledge.retriever", { kind: "choice", choices: ["dense", "hybrid"], default: "dense", value: "dense" }),
    setting("sandbox.default_execution", { kind: "choice", choices: ["sandbox", "host"], default: "sandbox", value: "sandbox" }),
    setting("tools.web_search", { kind: "choice", choices: ["auto", "tavily", "brave", "off"], default: "auto", value: "auto" }),
  ];
  return {
    paths: { user: "/home/me/.wright/settings.json", project: "/repo/.wright/settings.json" },
    settings: base.map((item) => ({ ...item, ...patch[item.key] })),
    warnings: [],
    models: models ?? undefined,
    environment: [{ name: "WRIGHT_HOME", value: null, description: "" }],
  };
}

const secrets = (patch: Record<string, SecretsView["secrets"][number]["source"]> = {}): SecretsView => ({
  path: "/home/me/.wright/.env",
  secrets: ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY", "SILICONFLOW_API_KEY"].map((name) => ({
    name, group: "models", source: patch[name] ?? (name === "ANTHROPIC_API_KEY" ? "file" : "missing"),
  })),
});

const permissions = (mode = "default"): PermissionDefaultsView => ({
  mode, modes: ["default", "acceptEdits", "bypass", "plan"], path: "/home/me/.wright/permission_settings.json", project: true,
  rules: [
    { id: "b1", effect: "deny", scope: "user", text: "shell(rm -* /)", description: "", locked: true },
    { id: "a1", effect: "ask", scope: "project", text: "shell(git push*)", description: "", locked: false },
  ],
});

const skills = (enabled = true): SkillSwitchesView => ({
  skills: [{ id: "lint", name: "lint", description: "Run lint", enabled }], errors: [], locked: false,
});

const mcp = (servers: McpConfigView["servers"] = []): McpConfigView => ({
  project_trusted: false, files: { user: { path: "/home/me/.wright/mcp.json", error: null } }, servers,
});

type Call = { url: string; method: string; body: unknown };

/**
 * A fake server: each resource answers GET with its first response and each
 * write with the next one. ``settings`` lists runtime settings views.
 */
function stubServer(responses: {
  settings?: RuntimeSettingsView[];
  secrets?: SecretsView[];
  permissions?: PermissionDefaultsView[];
  skills?: SkillSwitchesView[];
  mcp?: McpConfigView[];
} = {}) {
  const calls: Call[] = [];
  const counters: Record<string, number> = {};
  const pick = <T,>(name: string, list: T[] | undefined, fallback: T, write: boolean): T => {
    const items = list ?? [fallback];
    counters[name] = (counters[name] ?? 0) + (write ? 1 : 0);
    return items[Math.min(counters[name], items.length - 1)];
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const write = method !== "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    let body: unknown;
    if (url.includes("/settings/secrets")) body = pick("secrets", responses.secrets, secrets(), write);
    else if (url.includes("/settings/permissions")) body = pick("permissions", responses.permissions, permissions(), write);
    else if (url.includes("/settings/skills")) body = pick("skills", responses.skills, skills(), write);
    else if (url.includes("/settings/mcp")) body = pick("mcp", responses.mcp, mcp(), write);
    else if (url.includes("/settings/hooks")) body = { path: "", error: null, hooks: [] };
    else body = pick("settings", responses.settings, view(), write);
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

const writes = (calls: Call[]) => calls.filter((call) => call.method !== "GET").map(({ url, method, body }) => ({ url, method, body }));
const settingWrites = (calls: Call[]) => writes(calls).filter((call) => /\/settings$/.test(call.url)).map((call) => call.body);
const page = (projectId: string | null = null) => render(<SettingsPage theme="light" inspectorOpen save={async () => undefined} projectId={projectId} />);
const openSection = (name: string) => fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name }));

describe("SettingsPage rows", () => {
  it("saves how thinking is shown from the appearance card", async () => {
    stubServer();
    const save = vi.fn(async () => undefined);
    render(<SettingsPage theme="light" thoughtStyle="quiet" inspectorOpen save={save} projectId={null} />);
    expect((await screen.findByRole("radio", { name: "Quiet" })).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("radio", { name: "Prominent" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ thought_style: "lead" }));
  });

  it("locks keys the environment sets and lists them once at the top", async () => {
    stubServer({ settings: [view({ "llm.model": { value: "env-model", source: "env", env_override: true } })] });
    page("ws");
    expect(await screen.findByText("1 settings are set by environment variables")).toBeTruthy();
    openSection("Models");
    expect(screen.getAllByLabelText("Set by OPENAI_MODEL in the environment").length).toBeGreaterThan(0);
    expect((screen.getByRole("combobox", { name: "Model" }) as HTMLInputElement).disabled).toBe(true);
  });

  it("writes the user file by default and the project file when asked", async () => {
    const calls = stubServer({ settings: [view(), view({ "llm.model": { value: "p", source: "project", project_value: "p" } })] });
    page("ws");
    openSection("Models");
    const model = await screen.findByRole("combobox", { name: "Model" });
    expect(calls[0].url).toBe("/api/v1/workspaces/ws/settings");

    fireEvent.click(screen.getByRole("button", { name: "More for Model" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Set for this project only/ }));
    expect(screen.getByText("This project")).toBeTruthy();
    fireEvent.change(model, { target: { value: "p" } });
    fireEvent.keyDown(model, { key: "Enter" });
    await vi.waitFor(() => expect(settingWrites(calls)).toHaveLength(1));
    expect(settingWrites(calls)).toEqual([{ scope: "project", key: "llm.model", value: "p" }]);
  });

  it("writes booleans and choices, and resets a key to null", async () => {
    const endpoint = { user_value: "http://127.0.0.1:9/v1", value: "http://127.0.0.1:9/v1", source: "user" as const };
    const calls = stubServer({ settings: [view({ "llm.base_url": endpoint })] });
    page();
    openSection("Models");
    await screen.findByRole("button", { name: "API" });
    expect(calls[0].url).toBe("/api/v1/settings");
    // Without a project nothing can be set for one.
    expect(screen.queryByRole("menuitem", { name: /this project only/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "API" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Chat Completions" }));
    fireEvent.click(screen.getByRole("button", { name: "More for Endpoint" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Reset" }));
    openSection("General");
    fireEvent.click(screen.getByRole("switch", { name: "Name sessions automatically" }));
    await vi.waitFor(() => expect(settingWrites(calls)).toHaveLength(3));
    expect(settingWrites(calls)).toEqual([
      { scope: "user", key: "llm.transport", value: "chat" },
      { scope: "user", key: "llm.base_url", value: null },
      { scope: "user", key: "session.auto_title", value: false },
    ]);
  });

  it("shows a server refusal", async () => {
    stubServer();
    page();
    openSection("Context & cost");
    const limit = await screen.findByLabelText("Context limit");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "llm.context_limit must be at least 0" }), { status: 400 })));
    fireEvent.change(limit, { target: { value: "-1" } });
    fireEvent.blur(limit);
    expect((await screen.findByRole("alert")).textContent).toBe("llm.context_limit must be at least 0");
  });
});

describe("SettingsPage models", () => {
  it("shows provider readiness and saves a key without ever showing it", async () => {
    const calls = stubServer({ secrets: [secrets(), secrets({ OPENAI_API_KEY: "file" })] });
    page();
    openSection("Models");
    expect(await screen.findByText("Ready")).toBeTruthy();
    const openai = document.querySelector('[data-secret="OPENAI_API_KEY"]') as HTMLElement;
    fireEvent.click(within(openai).getByRole("button", { name: "Add key" }));
    const field = within(openai).getByLabelText("Value for OPENAI_API_KEY") as HTMLInputElement;
    expect(field.type).toBe("password");
    fireEvent.change(field, { target: { value: "sk-test" } });
    fireEvent.click(within(openai).getByRole("button", { name: "Save key" }));
    await vi.waitFor(() => expect(within(openai).getByText("Saved")).toBeTruthy());
    expect(writes(calls).find((call) => call.url.includes("/secrets/"))).toEqual({
      url: "/api/v1/settings/secrets/OPENAI_API_KEY", method: "PUT", body: { value: "sk-test" },
    });
    expect(document.body.textContent).not.toContain("sk-test");
    // The model overview reloads so the provider status follows the new key.
    expect(calls.filter((call) => call.url === "/api/v1/settings" && call.method === "GET").length).toBeGreaterThan(1);
  });

  it("switches to one endpoint and back, clearing the endpoint after asking", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const endpoint = { user_value: "http://gw/v1", value: "http://gw/v1", source: "user" as const };
    const calls = stubServer({ settings: [view({ "llm.base_url": endpoint }), view()] });
    page();
    openSection("Models");
    expect(await screen.findByRole("radio", { name: "Custom endpoint", checked: true })).toBeTruthy();
    expect(screen.queryByText("Anthropic")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Provider keys" }));
    await vi.waitFor(() => expect(settingWrites(calls)).toEqual([{ scope: "user", key: "llm.base_url", value: null }]));
    expect(await screen.findByText("Anthropic")).toBeTruthy();
  });

  it("picks a catalog model, reachable providers first", async () => {
    const calls = stubServer();
    page();
    openSection("Models");
    const model = await screen.findByRole("combobox", { name: "Model" });
    fireEvent.focus(model);
    const options = within(screen.getByRole("listbox", { name: "Model" })).getAllByRole("option");
    expect(options[0].textContent).toContain("claude-opus-5-5");
    expect(options[0].textContent).toContain("1M");
    fireEvent.change(model, { target: { value: "gpt" } });
    expect(within(screen.getByRole("listbox", { name: "Model" })).getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(model, { key: "ArrowDown" });
    fireEvent.keyDown(model, { key: "Enter" });
    await vi.waitFor(() => expect(settingWrites(calls)).toEqual([{ scope: "user", key: "llm.model", value: "gpt-4o" }]));
  });

  it("adds and removes models offered by the picker", async () => {
    const calls = stubServer({ settings: [view(), view({ "llm.models": { user_value: ["gpt-4o"], value: ["gpt-4o"], source: "user" } }), view()] });
    page();
    openSection("Models");
    expect(await screen.findByText(/Using current models of your providers: claude-opus-5-5/)).toBeTruthy();
    const add = screen.getByRole("combobox", { name: "Add a model" });
    fireEvent.change(add, { target: { value: "gpt-4o" } });
    fireEvent.keyDown(add, { key: "Enter" });
    fireEvent.click(await screen.findByRole("button", { name: "Remove gpt-4o" }));
    await vi.waitFor(() => expect(settingWrites(calls)).toEqual([
      { scope: "user", key: "llm.models", value: ["gpt-4o"] },
      { scope: "user", key: "llm.models", value: null },
    ]));
  });

  it("still works with a server that sends no provider overview", async () => {
    stubServer({ settings: [view({}, null)] });
    page();
    openSection("Models");
    expect(await screen.findByRole("combobox", { name: "Model" })).toBeTruthy();
    expect(screen.queryByText("Ready")).toBeNull();
  });

  it("warns when the chosen model would stop new sessions", async () => {
    stubServer({ settings: [view(
      { "llm.memory_model": { value: "gpt-4o", user_value: "gpt-4o", source: "user" } },
      { ...overview, problems: { "llm.memory_model": "Set OPENAI_API_KEY in .env to use OpenAI (gpt-4o)" } },
    )] });
    page();
    openSection("Models");
    expect(await screen.findByText("New sessions will not start: Set OPENAI_API_KEY in .env to use OpenAI (gpt-4o)")).toBeTruthy();
  });
});

describe("SettingsPage prices", () => {
  it("saves valid rows and holds back an invalid one", async () => {
    const calls = stubServer({ settings: [view({ "llm.prices": { user_value: ["kimi-k3=3/15"], value: ["kimi-k3=3/15"], source: "user" } })] });
    page();
    openSection("Context & cost");
    await screen.findByLabelText("Input $/M 1");
    expect((screen.getByLabelText("Model 1") as HTMLInputElement).value).toBe("kimi-k3");

    fireEvent.click(screen.getByRole("button", { name: "Add a price" }));
    fireEvent.change(screen.getByLabelText("Model 2"), { target: { value: "mine" } });
    fireEvent.change(screen.getByLabelText("Input $/M 2"), { target: { value: "abc" } });
    fireEvent.keyDown(screen.getByLabelText("Input $/M 2"), { key: "Enter" });
    expect((await screen.findByRole("alert")).textContent).toMatch(/must be numbers/);
    expect(settingWrites(calls)).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Input $/M 2"), { target: { value: "0.5" } });
    fireEvent.change(screen.getByLabelText("Output $/M 2"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Cache read $/M 2"), { target: { value: "0.1" } });
    fireEvent.keyDown(screen.getByLabelText("Cache read $/M 2"), { key: "Enter" });
    await vi.waitFor(() => expect(settingWrites(calls)).toEqual([
      { scope: "user", key: "llm.prices", value: ["kimi-k3=3/15", "mine=0.5/2/0.1"] },
    ]));
  });
});

describe("SettingsPage permissions", () => {
  it("asks before a mode that stops asking, and keeps built-in rules", async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    const calls = stubServer({ permissions: [permissions(), permissions("bypass")] });
    page("ws");
    openSection("Permissions");
    fireEvent.click(await screen.findByRole("button", { name: "Permission mode" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: /Don't ask/ }));
    await vi.waitFor(() => expect(writes(calls)).toEqual([
      { url: "/api/v1/workspaces/ws/settings/permissions/mode", method: "PUT", body: { mode: "bypass" } },
    ]));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Remove shell(rm -* /)" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove shell(git push*)" })).toBeTruthy();
  });

  it("adds a deny rule for this project", async () => {
    const calls = stubServer();
    page("ws");
    openSection("Permissions");
    fireEvent.change(await screen.findByRole("textbox", { name: "Rule" }), { target: { value: "shell(terraform destroy*)" } });
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await vi.waitFor(() => expect(writes(calls)).toEqual([{
      url: "/api/v1/workspaces/ws/settings/permissions/rules", method: "POST",
      body: { effect: "deny", scope: "project", text: "shell(terraform destroy*)" },
    }]));
  });

  it("asks before running commands on the host", async () => {
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    const calls = stubServer();
    page();
    openSection("Permissions");
    fireEvent.click(await screen.findByRole("button", { name: "Where commands run" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Host" }));
    await vi.waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(settingWrites(calls)).toHaveLength(0);
  });
});

describe("SettingsPage tools", () => {
  it("turns a skill off and adds an MCP server without echoing its values", async () => {
    const saved = mcp([{
      name: "docs", scope: "user", transport: "stdio", command: "npx", args: ["docs"], url: "", env: ["TOKEN"], headers: [],
      problem: null, shadowed: false, active: true,
    }]);
    const calls = stubServer({ skills: [skills(), skills(false)], mcp: [mcp(), saved] });
    page();
    openSection("Tools");
    fireEvent.click(await screen.findByRole("switch", { name: "Turn lint on or off" }));
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "docs" } });
    fireEvent.change(screen.getByLabelText("Command and arguments, one per line"), { target: { value: "npx\ndocs" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    const pairName = screen.getAllByRole("textbox", { name: "Name" }).at(-1) as HTMLElement;
    fireEvent.change(pairName, { target: { value: "TOKEN" } });
    fireEvent.change(screen.getByLabelText("Value for TOKEN"), { target: { value: "t0ken" } });
    fireEvent.click(screen.getByRole("button", { name: "Save server" }));
    expect(await screen.findByText("Stored values: TOKEN")).toBeTruthy();
    expect(document.body.textContent).not.toContain("t0ken");
    expect(writes(calls)).toEqual([
      { url: "/api/v1/settings/skills/lint", method: "PUT", body: { enabled: false } },
      { url: "/api/v1/settings/mcp/docs", method: "PUT", body: { scope: "user", transport: "stdio", command: "npx", args: ["docs"], env: { TOKEN: "t0ken" } } },
    ]);
  });
});

describe("SettingsPage navigation", () => {
  it("searches across sections and hides gated options until turned on", async () => {
    stubServer();
    page();
    openSection("Knowledge search");
    expect(await screen.findByText(/Turn on knowledge search/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retriever" })).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "retriever" } });
    expect(screen.getByRole("button", { name: "Retriever" })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Model" })).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "theme" } });
    expect(screen.getByRole("radio", { name: "Dark" })).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "TAVILY" } });
    expect(document.querySelector('[data-secret="TAVILY_API_KEY"]')).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "zzz" } });
    expect(screen.getByText(/No setting matches/)).toBeTruthy();
  });

  it("shows configuration keys only when asked", async () => {
    stubServer();
    page();
    openSection("Models");
    await screen.findByRole("combobox", { name: "Model" });
    expect(screen.queryByText("llm.model")).toBeNull();
    openSection("Advanced");
    fireEvent.click(screen.getByRole("switch", { name: "Show configuration keys" }));
    expect(screen.getByText("WRIGHT_HOME")).toBeTruthy();
    openSection("Models");
    expect(screen.getByText("llm.model")).toBeTruthy();
  });
});

describe("SettingsPage memory", () => {
  it("turns memory parts off and folds them away when memory is off", async () => {
    const calls = stubServer({ settings: [view(), view({ "memory.enabled": { value: false, user_value: false, source: "user" } })] });
    page();
    openSection("Memory");
    fireEvent.click(await screen.findByRole("switch", { name: "Recall each turn" }));
    await vi.waitFor(() => expect(settingWrites(calls)).toEqual([{ scope: "user", key: "memory.recall", value: false }]));
    expect(await screen.findByText(/Memory is off/)).toBeTruthy();
    expect(screen.queryByRole("switch", { name: "Recall each turn" })).toBeNull();
  });
});
