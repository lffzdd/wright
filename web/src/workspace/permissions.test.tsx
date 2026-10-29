import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { setLocale } from "../i18n";
import type { Grants, Interaction } from "../types";
import { InteractionCard } from "./cards";
import { PermissionGrants } from "./permissions";
import { describeChoice, quickChoice } from "./approval";
import { describeGrant } from "./permissions";

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

it("offers the narrowest useful session rule as a one-click choice", async () => {
  const respond = vi.fn().mockResolvedValue(true);
  const sessionChoices: Interaction["choices"] = [
    ...(choices ?? []).slice(0, 1),
    { id: "allow_session_rule", label: "Session", scope: "/project/src/a.ts", persistence: "", lifetime: "session", resource_kind: "file", operations: ["file_read", "file_write"] },
    { id: "allow_session_directory_write", label: "Session", scope: "/project/src", persistence: "", lifetime: "session", resource_kind: "directory", operations: ["file_read", "file_write"] },
    ...(choices ?? []).slice(1),
  ];
  render(<InteractionCard sessionId="s" root="/project" interaction={{ request_id: "r", kind: "permission", tool_name: "edit_file", operation: "file_write", targets: ["/project/src/a.ts"], choices: sessionChoices }} respond={respond} />);
  expect(screen.getByText("Wright wants to change a file")).toBeTruthy();
  expect(document.querySelector(".perm-target")?.textContent).toBe("src/a.ts");
  fireEvent.click(screen.getByRole("button", { name: /Allow edits to everything in src\/ for this session/ }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("r", "allow_session_directory_write"));
});

it("keeps outside-project access narrow: the quick rule is the single file", async () => {
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" root="/project" interaction={{
    request_id: "r", kind: "permission", tool_name: "read_file", operation: "file_read", targets: ["/etc/hosts"], risk_flags: ["reads_files", "path_outside_scope"],
    choices: [
      { id: "allow_once", label: "Once", scope: "", persistence: "", lifetime: "once" },
      { id: "allow_session_rule", label: "", scope: "/etc/hosts", persistence: "", lifetime: "session", resource_kind: "file", operations: ["file_read"] },
      { id: "allow_session_directory_read", label: "", scope: "/etc", persistence: "", lifetime: "session", resource_kind: "directory", operations: ["file_read"] },
      { id: "deny", label: "Deny", scope: "", persistence: "" },
    ],
  }} respond={respond} />);
  expect(screen.getByText("Outside this project")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /Allow reads of \/etc\/hosts for this session/ }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("r", "allow_session_rule"));
});

it("requires an explicit selection for project authority and hides global authority", async () => {
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={{ request_id: "r", kind: "permission", choices }} respond={respond} />);
  expect(screen.queryByRole("radio")).toBeNull();
  fireEvent.click(screen.getByText("More options"));
  const radios = screen.getAllByRole("radio") as HTMLInputElement[];
  expect(radios).toHaveLength(2);
  expect(radios.every((radio) => !radio.checked)).toBe(true);
  expect(screen.queryByText(/in all projects/)).toBeNull();
  expect(screen.getByRole("button", { name: "Save and allow" })).toHaveProperty("disabled", true);
  fireEvent.click(radios[0]);
  fireEvent.click(screen.getByText("Save and allow"));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("r", "allow_project_rule"));
});

it("uses server option IDs for explicit directory expansion and shows global rules only on request", async () => {
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={{ request_id: "r", kind: "permission", choices }} respond={respond} />);
  fireEvent.click(screen.getByText("More options"));
  fireEvent.click(screen.getByText("Show options for all projects"));
  expect(screen.getAllByRole("radio")).toHaveLength(3);
  fireEvent.click(screen.getByLabelText(/everything in \/project\//));
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
  expect(screen.getByText("Wright wants to change a file")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toContain("Approval applies to the complete content");
  expect(container.querySelector(".approval-target")?.textContent).not.toContain("$");
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
  expect(screen.getByRole("button", { name: /Allow “echo hello” for this session/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox"));
  // With network on, nothing that would be remembered is offered directly.
  expect(screen.queryByRole("button", { name: /for this session/ })).toBeNull();
  fireEvent.click(screen.getByText("More options"));
  expect(screen.queryByText("Show options for all projects")).toBeNull();
  const save = screen.getByRole("button", { name: "Save and allow" }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  expect(screen.getByText(/Network is for this invocation only/)).toBeTruthy();
  fireEvent.click(save);
  expect(respond).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /Allow once with network/i }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("shell", "allow_once_network"));
  expect(screen.getByRole("button", { name: /Submitting/ })).toBeTruthy();
});

it("shows only the facts that change the decision, localized", async () => {
  setLocale("zh-CN");
  vi.spyOn(api, "grants").mockResolvedValue(readyGrants);
  const { rerender, container } = render(<InteractionCard sessionId="s" interaction={shellInteraction} respond={vi.fn()} />);
  // "Runs a shell command" is already said by the title; no badge repeats it.
  expect(container.querySelectorAll(".approval-badge")).toHaveLength(0);
  rerender(<InteractionCard sessionId="s" interaction={{ ...shellInteraction, risk_level: "elevated", risk_flags: ["executes_shell", "deletes_files"] }} respond={vi.fn()} />);
  expect(screen.getByText("删除文件")).toBeTruthy();
  expect(container.querySelector(".approval-card.elevated")).toBeTruthy();
});

it("does not approve while the user is using a scope selector", () => {
  const respond = vi.fn();
  render(<InteractionCard sessionId="s" interaction={{ request_id: "file", kind: "permission", choices }} respond={respond} />);
  fireEvent.click(screen.getByText("More options"));
  fireEvent.keyDown(screen.getAllByRole("radio")[0], { key: "Enter", altKey: true });
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

const executableChoice = {
  id: "allow_session_command_executable", label: "Session", scope: "/usr/bin/python *", persistence: "Session",
  lifetime: "session", resource_kind: "command", operations: ["shell"],
  resource: { kind: "command", command_match: "executable", argv: ["/usr/bin/python"], cwd: "/project", sandbox_key: "offline", dialect: "posix" },
};

it("makes arbitrary interpreter arguments and subprocesses explicit before quick approval", async () => {
  vi.spyOn(api, "grants").mockResolvedValue(readyGrants);
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" root="/project" interaction={{ ...shellInteraction, command: "python -c 'print(1)'", choices: [...shellInteraction.choices!, executableChoice] }} respond={respond} />);
  expect(screen.getByText(/Includes running interpreter code and scripts/).textContent).toContain("launch subprocesses");
  expect(screen.getByText(/Working directory: \./).textContent).toContain("Offline");
  const button = screen.getByRole("button", { name: /Allow any arguments for \/usr\/bin\/python.*for this session/ });
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  fireEvent.click(button);
  await waitFor(() => expect(respond).toHaveBeenCalledWith("shell", executableChoice.id));
});

it("defaults to the session rule, preserving exact commands and explicit long-term choices", async () => {
  vi.spyOn(api, "grants").mockResolvedValue(readyGrants);
  const respond = vi.fn().mockResolvedValue(true);
  const project = { ...executableChoice, id: "allow_project_command_executable", lifetime: "project" };
  render(<InteractionCard sessionId="s" interaction={{ ...shellInteraction, choices: [...shellInteraction.choices!, executableChoice, project] }} respond={respond} />);
  fireEvent.click(screen.getByText("More options"));
  expect(screen.getByLabelText(/Any arguments for \/usr\/bin\/python · for this session/)).toHaveProperty("checked", true);
  expect(screen.getByLabelText(/Any arguments for \/usr\/bin\/python · in this project/)).toHaveProperty("checked", false);
  const exact = screen.getByLabelText(/“echo hello” for this session/);
  fireEvent.click(exact);
  await waitFor(() => expect(screen.getByRole("button", { name: "Save and allow" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "Save and allow" }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("shell", "allow_session_rule"));
});

it("recommends a bounded argument prefix when offered without hiding the executable option", () => {
  const prefix = { ...executableChoice, id: "prefix", resource: { ...executableChoice.resource, argv: ["/usr/bin/git", "status"], command_match: "argv_prefix" } };
  expect(quickChoice([executableChoice, prefix], false)?.id).toBe("prefix");
  expect(describeChoice(prefix)).toContain("any following arguments for /usr/bin/git status");
  expect(describeChoice(executableChoice)).toContain("any arguments for /usr/bin/python");
});

it("distinguishes HTTP origin from exact path and recommends all paths with the current method", async () => {
  const respond = vi.fn().mockResolvedValue(true);
  const origin = { id: "allow_session_rule", label: "Session", scope: "GET https://example.com:443", persistence: "", lifetime: "session", resource_kind: "network", operations: ["network_read"], resource: { kind: "network", scheme: "https", host: "example.com", port: 443, methods: ["GET"], path_match: "origin" } };
  const path = { ...origin, id: "allow_session_network_path", scope: "GET https://example.com:443/api", resource: { ...origin.resource, path_match: "exact", path: "/api" } };
  render(<InteractionCard sessionId="s" interaction={{ request_id: "http", kind: "permission", tool_name: "http_request", http_method: "GET", http_target: "https://example.com/api", choices: [{ id: "allow_once", label: "Once", scope: "", persistence: "" }, path, origin] }} respond={respond} />);
  expect(screen.getByRole("button", { name: /Allow GET requests to all paths at https:\/\/example.com:443/ })).toBeTruthy();
  fireEvent.click(screen.getByText("More options"));
  fireEvent.click(screen.getByLabelText(/GET requests to the exact path https:\/\/example.com:443\/api/));
  fireEvent.click(screen.getByRole("button", { name: "Save and allow" }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("http", path.id));
});

it("renders and revokes command, HTTP path and named tool records using server identity", async () => {
  vi.spyOn(api, "revokeGrant").mockResolvedValue({ ...readyGrants, grants: [] });
  const records: Grants = { ...readyGrants, grants: [
    { id: "python-id", source: "project", lifetime: "project", resource_kind: "command", target: "/usr/bin/python", operations: ["shell"], recursive: false, cwd: "/project", resource: executableChoice.resource },
    { id: "http-id", source: "session", lifetime: "session", resource_kind: "network", target: "https://example.com/api", http_methods: ["GET"], operations: ["network_read"], recursive: false, resource: { path_match: "exact", methods: ["GET"] } },
    { id: "search-id", source: "user", lifetime: "user", resource_kind: "tool", target: "web_search", tool: "web_search", operations: ["network_read"], recursive: false },
  ] };
  render(<PermissionGrants sessionId="s" grants={records} update={vi.fn()} />);
  expect(screen.getByText("any arguments for /usr/bin/python")).toBeTruthy();
  expect(screen.getByText("GET requests to the exact path https://example.com/api (any query parameters)")).toBeTruthy();
  expect(screen.getByText("Use web_search for the listed operations")).toBeTruthy();
  expect(screen.getByText("Allowed operations: Network read")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Revoke authorization for /usr/bin/python" }));
  await waitFor(() => expect(api.revokeGrant).toHaveBeenCalledWith("s", "python-id", "project", "revision-a"));
});

it("localizes broad program, HTTP path and recursive directory scope in Chinese", () => {
  setLocale("zh-CN");
  expect(describeChoice(executableChoice)).toContain("任意参数运行 /usr/bin/python");
  expect(describeGrant({ id: "http", source: "session", lifetime: "session", resource_kind: "network", target: "https://example.com/api", operations: ["network_read"], recursive: false, resource: { path_match: "exact", methods: ["GET"] } })).toBe("仅对路径 https://example.com/api 的 GET 请求（查询参数不限）");
  render(<InteractionCard sessionId="s" interaction={{ request_id: "file", kind: "permission", choices }} respond={vi.fn()} />);
  fireEvent.click(screen.getByText("更多选项"));
  expect(screen.getByText("包括此目录和所有子目录中的全部文件。")).toBeTruthy();
});
