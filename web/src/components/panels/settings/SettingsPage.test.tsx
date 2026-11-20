import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ModelOverview, RuntimeSetting, RuntimeSettingsView } from "../../../types";
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
  ];
  return {
    paths: { user: "/home/me/.wright/settings.json", project: "/repo/.wright/settings.json" },
    settings: base.map((item) => ({ ...item, ...patch[item.key] })),
    warnings: [],
    models: models ?? undefined,
  };
}

function stubFetch(responses: RuntimeSettingsView[]) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const body = responses[Math.min(calls.length - 1, responses.length - 1)];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

const bodies = (calls: Array<{ init?: RequestInit }>) => calls.slice(1).map((call) => JSON.parse(String(call.init?.body)));
const page = (projectId: string | null = null) => render(<SettingsPage theme="light" inspectorOpen save={async () => undefined} projectId={projectId} />);
const openSection = (name: string) => fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name }));

describe("SettingsPage runtime settings", () => {
  it("shows where a value came from and edits the chosen file", async () => {
    const calls = stubFetch([
      view({ "llm.model": { value: "env-model", source: "env", env_override: true } }),
      view({ "llm.model": { value: "p", source: "project", project_value: "p" } }),
    ]);
    page("ws");
    expect(await screen.findByText(/OPENAI_MODEL in the environment wins/)).toBeTruthy();
    expect(calls[0].url).toBe("/api/v1/workspaces/ws/settings");

    fireEvent.click(screen.getByRole("radio", { name: "Project" }));
    expect(screen.getByText("Editing /repo/.wright/settings.json")).toBeTruthy();
    // User-only keys are read-only in project scope.
    expect(screen.getAllByText("Only the user file can set this").length).toBeGreaterThan(0);

    const model = screen.getByRole("combobox", { name: "Model" });
    fireEvent.change(model, { target: { value: "p" } });
    fireEvent.keyDown(model, { key: "Enter" });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].init?.method).toBe("PUT");
    expect(bodies(calls)).toEqual([{ scope: "project", key: "llm.model", value: "p" }]);
    expect(await screen.findByText("The project file sets 1")).toBeTruthy();
  });

  it("writes booleans and choices, and resets a key to null", async () => {
    const calls = stubFetch([view({ "llm.base_url": { user_value: "http://127.0.0.1:9/v1", value: "http://127.0.0.1:9/v1", source: "user" } })]);
    page();
    await screen.findByText("Custom endpoint");
    expect(calls[0].url).toBe("/api/v1/settings");
    expect(screen.queryByRole("radio", { name: "Project" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "API" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Chat Completions" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    openSection("Sessions");
    fireEvent.click(screen.getByRole("switch", { name: "Name sessions automatically" }));
    await vi.waitFor(() => expect(calls).toHaveLength(4));
    expect(bodies(calls)).toEqual([
      { scope: "user", key: "llm.transport", value: "chat" },
      { scope: "user", key: "llm.base_url", value: null },
      { scope: "user", key: "session.auto_title", value: false },
    ]);
  });

  it("shows a server refusal", async () => {
    stubFetch([view()]);
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
  it("shows provider key status without the key, and picks a catalog model", async () => {
    const calls = stubFetch([view()]);
    page();
    expect(await screen.findByText("Ready")).toBeTruthy();
    expect(screen.getByText("No key")).toBeTruthy();
    expect(screen.getByText(/ANTHROPIC_API_KEY/)).toBeTruthy();

    const model = screen.getByRole("combobox", { name: "Model" });
    fireEvent.focus(model);
    const options = within(screen.getByRole("listbox", { name: "Model" })).getAllByRole("option");
    // Reachable providers come first.
    expect(options[0].textContent).toContain("claude-opus-5-5");
    expect(options[0].textContent).toContain("1M");
    fireEvent.change(model, { target: { value: "gpt" } });
    expect(within(screen.getByRole("listbox", { name: "Model" })).getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(model, { key: "ArrowDown" });
    fireEvent.keyDown(model, { key: "Enter" });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(bodies(calls)).toEqual([{ scope: "user", key: "llm.model", value: "gpt-4o" }]);
  });

  it("adds and removes models offered by the picker", async () => {
    const calls = stubFetch([view(), view({ "llm.models": { user_value: ["gpt-4o"], value: ["gpt-4o"], source: "user" } })]);
    page();
    expect(await screen.findByText(/Using current models of your providers: claude-opus-5-5/)).toBeTruthy();
    const add = screen.getByRole("combobox", { name: "Add a model" });
    fireEvent.change(add, { target: { value: "gpt-4o" } });
    fireEvent.keyDown(add, { key: "Enter" });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    fireEvent.click(await screen.findByRole("button", { name: "Remove gpt-4o" }));
    await vi.waitFor(() => expect(calls).toHaveLength(3));
    expect(bodies(calls)).toEqual([
      { scope: "user", key: "llm.models", value: ["gpt-4o"] },
      { scope: "user", key: "llm.models", value: null },
    ]);
  });

  it("still works with a server that sends no provider overview", async () => {
    stubFetch([view({}, null)]);
    page();
    expect(await screen.findByRole("combobox", { name: "Model" })).toBeTruthy();
    expect(screen.queryByText("Ready")).toBeNull();
  });
});

describe("SettingsPage prices", () => {
  it("saves valid rows and holds back an invalid one", async () => {
    const calls = stubFetch([view({ "llm.prices": { user_value: ["kimi-k3=3/15"], value: ["kimi-k3=3/15"], source: "user" } })]);
    page();
    openSection("Context & cost");
    const input = await screen.findByLabelText("Input $/M 1");
    expect((screen.getByLabelText("Model 1") as HTMLInputElement).value).toBe("kimi-k3");

    fireEvent.click(screen.getByRole("button", { name: "Add a price" }));
    fireEvent.change(screen.getByLabelText("Model 2"), { target: { value: "mine" } });
    fireEvent.change(screen.getByLabelText("Input $/M 2"), { target: { value: "abc" } });
    fireEvent.keyDown(screen.getByLabelText("Input $/M 2"), { key: "Enter" });
    expect((await screen.findByRole("alert")).textContent).toMatch(/must be numbers/);
    expect(calls).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("Input $/M 2"), { target: { value: "0.5" } });
    fireEvent.change(screen.getByLabelText("Output $/M 2"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Cache read $/M 2"), { target: { value: "0.1" } });
    fireEvent.keyDown(screen.getByLabelText("Cache read $/M 2"), { key: "Enter" });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(bodies(calls)).toEqual([{ scope: "user", key: "llm.prices", value: ["kimi-k3=3/15", "mine=0.5/2/0.1"] }]);
    void input;
  });
});

describe("SettingsPage navigation", () => {
  it("searches across sections and hides gated options until turned on", async () => {
    stubFetch([view()]);
    page();
    await screen.findByRole("combobox", { name: "Model" });
    openSection("Knowledge search");
    expect(screen.getByText(/Turn on knowledge search/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retriever" })).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "retriever" } });
    expect(screen.getByRole("button", { name: "Retriever" })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Model" })).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "theme" } });
    expect(screen.getByRole("radio", { name: "Dark" })).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "zzz" } });
    expect(screen.getByText(/No setting matches/)).toBeTruthy();
  });

  it("shows configuration keys only when asked", async () => {
    stubFetch([view()]);
    page();
    await screen.findByRole("combobox", { name: "Model" });
    expect(screen.queryByText("llm.model")).toBeNull();
    openSection("Advanced");
    fireEvent.click(screen.getByRole("switch", { name: "Show configuration keys" }));
    openSection("Models");
    expect(screen.getByText("llm.model")).toBeTruthy();
  });
});

describe("SettingsPage memory", () => {
  it("turns memory parts off and folds them away when memory is off", async () => {
    const calls = stubFetch([view(), view({ "memory.enabled": { value: false, user_value: false, source: "user" } })]);
    page();
    await screen.findByRole("combobox", { name: "Model" });
    openSection("Memory");
    fireEvent.click(screen.getByRole("switch", { name: "Recall each turn" }));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(bodies(calls)).toEqual([{ scope: "user", key: "memory.recall", value: false }]);

    expect(await screen.findByText(/Memory is off/)).toBeTruthy();
    expect(screen.queryByRole("switch", { name: "Recall each turn" })).toBeNull();
    // The memory model also names sessions and judges the egress check.
    expect(screen.getByRole("combobox", { name: "Memory model" })).toBeTruthy();
  });

  it("warns when the chosen model would stop new sessions", async () => {
    stubFetch([view(
      { "llm.memory_model": { value: "gpt-4o", user_value: "gpt-4o", source: "user" } },
      { ...overview, problems: { "llm.memory_model": "Set OPENAI_API_KEY in .env to use OpenAI (gpt-4o)" } },
    )]);
    page();
    await screen.findByRole("combobox", { name: "Model" });
    expect(screen.queryByText(/New sessions will not start/)).toBeNull();
    openSection("Memory");
    expect(screen.getByText("New sessions will not start: Set OPENAI_API_KEY in .env to use OpenAI (gpt-4o)")).toBeTruthy();
  });
});
