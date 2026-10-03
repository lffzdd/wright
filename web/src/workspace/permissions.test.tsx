import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { setLocale } from "../i18n";
import type { Grants, Interaction } from "../types";
import { InteractionCard } from "./cards";
import { PermissionGrants } from "./permissions";

const choices: Interaction["choices"] = [
  { id: "allow_once", label: "Once", scope: "/project/a", persistence: "", lifetime: "once" },
  { id: "allow_project_rule", label: "Project", scope: "/project/a", persistence: "", lifetime: "project", resource_kind: "file", operations: ["file_read", "file_write"] },
  { id: "allow_project_directory_write", label: "Project", scope: "/project", persistence: "", lifetime: "project", resource_kind: "directory", operations: ["file_read", "file_write"] },
  { id: "allow_user_rule", label: "User", scope: "/project/a", persistence: "", lifetime: "user", resource_kind: "file", operations: ["file_read", "file_write"] },
  { id: "deny", label: "Deny", scope: "", persistence: "" },
];
const grants: Grants = { effective_policy: [], version: "revision-a", grants: [{ id: "stable", source: "project", lifetime: "project", resource_kind: "file", target: "/project/a", operations: ["file_read"], recursive: false, tool: "*", version: "revision-a" }], sandbox: { available: false, state: "setup_required", provider: "Windows", platform: "win32" } };
beforeEach(() => setLocale("en"));
afterEach(() => { cleanup(); vi.restoreAllMocks(); setLocale("en"); });

it("defaults remembered approval to exact file and project and hides global authority", async () => {
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={{ request_id: "r", kind: "permission", choices }} respond={respond} />);
  expect(screen.queryByRole("combobox")).toBeNull();
  fireEvent.click(screen.getByText("Remember authorization"));
  const selections = screen.getAllByRole("combobox") as HTMLSelectElement[];
  expect(selections[0].value).toBe("file:/project/a");
  expect(selections[2].value).toBe("project");
  expect(screen.queryByRole("option", { name: "All projects long term" })).toBeNull();
  fireEvent.click(screen.getByText("Save and allow"));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("r", "allow_project_rule"));
});

it("uses server option IDs for explicit directory expansion", async () => {
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={{ request_id: "r", kind: "permission", choices }} respond={respond} />);
  fireEvent.click(screen.getByText("Remember authorization"));
  fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "directory:/project" } });
  fireEvent.click(screen.getByText("Save and allow"));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("r", "allow_project_directory_write"));
});

it("supports keyboard once approval and denial in Chinese", async () => {
  setLocale("zh-CN");
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={{ request_id: "r", kind: "permission", choices }} respond={respond} />);
  expect(screen.getByRole("button", { name: /拒绝/ })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("alert"), { key: "Enter", altKey: true });
  await waitFor(() => expect(respond).toHaveBeenCalledWith("r", "allow_once"));
});

it("revokes by source, ID and expected version and refreshes conflicts", async () => {
  const update = vi.fn();
  vi.spyOn(api, "revokeGrant").mockRejectedValue(new ApiError("Permissions changed", 409, { ...grants, version: "revision-b", grants: [] }));
  vi.spyOn(api, "grants").mockResolvedValue({ ...grants, version: "revision-b", grants: [] });
  render(<PermissionGrants sessionId="s" grants={grants} update={update} />);
  fireEvent.click(screen.getByText("Revoke"));
  await waitFor(() => expect(api.revokeGrant).toHaveBeenCalledWith("s", "stable", "project", "revision-a"));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Permissions changed. The latest state is shown; review it and retry.");
  await waitFor(() => expect(update).toHaveBeenCalledWith(expect.objectContaining({ version: "revision-b" })));
});

it("adds explicit resource access with project duration by default", async () => {
  const update = vi.fn();
  vi.spyOn(api, "addGrant").mockResolvedValue(grants);
  vi.spyOn(api, "grants").mockResolvedValue(grants);
  render(<PermissionGrants sessionId="s" grants={grants} update={update} />);
  fireEvent.change(screen.getByLabelText("Resource path"), { target: { value: "/external/file" } });
  fireEvent.click(screen.getByRole("button", { name: "Add authorization" }));
  await waitFor(() => expect(api.addGrant).toHaveBeenCalledWith("s", { kind: "file", path: "/external/file", operations: ["file_read"] }, "project", "revision-a"));
  await waitFor(() => expect(screen.getByLabelText("Resource path")).toHaveProperty("value", ""));
  expect(screen.getByText("Authorization saved.").getAttribute("role")).toBe("status");
});

const shellInteraction: Interaction = {
  request_id: "shell", kind: "permission", tool_name: "execute_command", command: "echo hello", operation: "shell",
  risk_level: "review", risk_flags: ["executes_shell"], cwd: "/project",
  choices: [
    { id: "allow_once", label: "Once", scope: "This invocation only", persistence: "", lifetime: "once" },
    { id: "allow_session_rule", label: "Session", scope: "echo hello · /project", persistence: "", lifetime: "session", resource_kind: "shell", operations: ["shell"] },
    { id: "allow_once_network", label: "With network", scope: "This command only", persistence: "" },
    { id: "deny", label: "Deny", scope: "", persistence: "" },
  ],
};
const readyGrants: Grants = { ...grants, sandbox: { available: true, state: "ready", provider: "Windows", platform: "win32" } };

it("keeps file target and content separate, including long paths and a shortened preview", () => {
  const path = "/external/" + "long-directory/".repeat(20) + "result.txt";
  const { container } = render(<InteractionCard sessionId="s" interaction={{ request_id: "file", kind: "permission", tool_name: "write_file", operation: "file_write", subject: path, targets: [path, path], preview: "new file content", preview_truncated: true, choices }} respond={vi.fn()} />);
  expect(container.querySelector(".perm-target")?.textContent).toBe(path);
  expect(container.querySelector(".permission-preview pre")?.textContent).toBe("new file content");
  expect(screen.getByText("Write or edit a file")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toContain("Approval applies to the complete content");
  expect(container.querySelector(".perm-command")?.textContent).not.toContain("$");
});

it("blocks Shell click and keyboard approval until setup is ready, then requires a new explicit approval", async () => {
  vi.spyOn(api, "grants").mockResolvedValue(grants);
  vi.spyOn(api, "sandbox").mockResolvedValue(readyGrants.sandbox!);
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={shellInteraction} respond={respond} />);
  const allow = screen.getByRole("button", { name: /Allow once/i }) as HTMLButtonElement;
  expect(allow.disabled).toBe(true);
  const initialize = await screen.findByRole("button", { name: "Initialize / retry (administrator)" });
  fireEvent.keyDown(screen.getByRole("alert"), { key: "Enter", altKey: true });
  fireEvent.click(allow);
  expect(respond).not.toHaveBeenCalled();
  fireEvent.click(initialize);
  await waitFor(() => expect(allow.disabled).toBe(false));
  expect(api.sandbox).toHaveBeenCalledWith("s", "setup");
  expect(respond).not.toHaveBeenCalled();
  fireEvent.click(allow);
  await waitFor(() => expect(respond).toHaveBeenCalledWith("shell", "allow_once"));
});

it("can deny a Shell request even when isolation is unavailable", async () => {
  vi.spyOn(api, "grants").mockResolvedValue(grants);
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={shellInteraction} respond={respond} />);
  fireEvent.click(screen.getByRole("button", { name: /Deny/ }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("shell", "deny"));
});

it("network opt-in sends the server network choice and cannot silently save offline authority", async () => {
  vi.spyOn(api, "grants").mockResolvedValue(readyGrants);
  const respond = vi.fn(() => new Promise<boolean>(() => undefined));
  render(<InteractionCard sessionId="s" interaction={shellInteraction} respond={respond} />);
  await waitFor(() => expect((screen.getByRole("button", { name: /Allow once/i }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText("Remember authorization"));
  expect(screen.queryByText("Advanced: all projects")).toBeNull();
  fireEvent.click(screen.getByRole("checkbox"));
  const save = screen.getByRole("button", { name: "Save and allow" }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  expect(screen.getByText(/Network is for this invocation only/)).toBeTruthy();
  fireEvent.click(save);
  expect(respond).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /Allow once with network/i }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("shell", "allow_once_network"));
  expect(screen.getByRole("button", { name: /Submitting/ })).toBeTruthy();
});

it("localizes the server risk level without assigning danger from generic capabilities", async () => {
  setLocale("zh-CN");
  vi.spyOn(api, "grants").mockResolvedValue(readyGrants);
  const { rerender } = render(<InteractionCard sessionId="s" interaction={shellInteraction} respond={vi.fn()} />);
  expect(screen.getByText("需要确认")).toBeTruthy();
  expect(screen.queryByText("Medium")).toBeNull();
  rerender(<InteractionCard sessionId="s" interaction={{ ...shellInteraction, risk_level: "elevated", risk_flags: ["deletes_files"] }} respond={vi.fn()} />);
  expect(screen.getByText("删除或修改外部状态")).toBeTruthy();
});

it("does not approve while the user is using a scope selector", () => {
  const respond = vi.fn();
  render(<InteractionCard sessionId="s" interaction={{ request_id: "file", kind: "permission", choices }} respond={respond} />);
  fireEvent.click(screen.getByText("Remember authorization"));
  fireEvent.keyDown(screen.getAllByRole("combobox")[0], { key: "Enter", altKey: true });
  expect(respond).not.toHaveBeenCalled();
});

it("ignores a previous session's delayed readiness when switching sessions", async () => {
  let finish: (value: Grants) => void = () => undefined;
  vi.spyOn(api, "grants").mockImplementation((sessionId) => sessionId === "old" ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(grants));
  const respond = vi.fn();
  const { rerender } = render(<InteractionCard sessionId="old" interaction={shellInteraction} respond={respond} />);
  rerender(<InteractionCard sessionId="new" interaction={shellInteraction} respond={respond} />);
  await screen.findByRole("button", { name: "Initialize / retry (administrator)" });
  finish(readyGrants);
  await waitFor(() => expect((screen.getByRole("button", { name: /Allow once/i }) as HTMLButtonElement).disabled).toBe(true));
  fireEvent.keyDown(screen.getByRole("alert"), { key: "Enter", altKey: true });
  expect(respond).not.toHaveBeenCalled();
});
