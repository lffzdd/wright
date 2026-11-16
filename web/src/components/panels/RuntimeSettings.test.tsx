import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RuntimeSetting, RuntimeSettingsView } from "../../types";
import { RuntimeSettings } from "./RuntimeSettings";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function setting(key: string, patch: Partial<RuntimeSetting>): RuntimeSetting {
  return {
    key, kind: "str", choices: [], minimum: null, description: "", default: "", value: "",
    source: "default", user_value: null, project_value: null, env: [], env_override: false,
    scopes: ["user"], ...patch,
  };
}

function view(patch: Partial<RuntimeSetting>[] = []): RuntimeSettingsView {
  const base = [
    setting("llm.model", { scopes: ["user", "project"], env: ["OPENAI_MODEL"] }),
    setting("llm.base_url", { env: ["OPENAI_BASE_URL"] }),
    setting("llm.transport", { kind: "choice", choices: ["auto", "chat", "responses"], default: "auto", value: "auto" }),
    setting("session.auto_title", { kind: "bool", default: true, value: true }),
  ];
  return {
    paths: { user: "/home/me/.wright/settings.json", project: "/repo/.wright/settings.json" },
    settings: base.map((item, index) => ({ ...item, ...patch[index] })),
    warnings: [],
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

describe("RuntimeSettings", () => {
  it("shows where each value came from and edits the chosen file", async () => {
    const calls = stubFetch([
      view([{ value: "env-model", source: "env", env_override: true }]),
      view([{ value: "p", source: "project", project_value: "p" }]),
    ]);
    render(<RuntimeSettings projectId="ws" />);
    expect(await screen.findByText("OPENAI_MODEL overrides this")).toBeTruthy();
    expect(calls[0].url).toBe("/api/v1/workspaces/ws/settings");

    fireEvent.click(screen.getByRole("radio", { name: "Project" }));
    expect(screen.getByText("Editing /repo/.wright/settings.json")).toBeTruthy();
    expect((screen.getByLabelText("Endpoint") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getAllByText("Only the user file can set this").length).toBe(3);

    const model = screen.getByLabelText("Model") as HTMLInputElement;
    fireEvent.change(model, { target: { value: "p" } });
    fireEvent.keyDown(model, { key: "Enter" });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].init?.method).toBe("PUT");
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({ scope: "project", key: "llm.model", value: "p" });
    expect(await screen.findByRole("button", { name: "Reset" })).toBeTruthy();
  });

  it("writes booleans and choices, and resets a key to null", async () => {
    const calls = stubFetch([view([{}, { user_value: "http://127.0.0.1:9/v1", value: "http://127.0.0.1:9/v1", source: "user" }])]);
    render(<RuntimeSettings projectId={null} />);
    await screen.findByLabelText("Endpoint");
    expect(calls[0].url).toBe("/api/v1/settings");
    expect(screen.queryByRole("radio", { name: "Project" })).toBeNull();

    fireEvent.click(screen.getByRole("switch", { name: "Name sessions automatically" }));
    fireEvent.click(screen.getByRole("button", { name: "API" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await vi.waitFor(() => expect(calls).toHaveLength(4));
    const bodies = calls.slice(1).map((call) => JSON.parse(String(call.init?.body)));
    expect(bodies).toEqual([
      { scope: "user", key: "session.auto_title", value: false },
      { scope: "user", key: "llm.transport", value: "chat" },
      { scope: "user", key: "llm.base_url", value: null },
    ]);
  });

  it("shows a server refusal", async () => {
    stubFetch([view()]);
    render(<RuntimeSettings projectId={null} />);
    const endpoint = await screen.findByLabelText("Endpoint");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Invalid llm.base_url" }), { status: 400 })));
    fireEvent.change(endpoint, { target: { value: "x" } });
    fireEvent.blur(endpoint);
    expect((await screen.findByRole("alert")).textContent).toBe("Invalid llm.base_url");
  });
});
