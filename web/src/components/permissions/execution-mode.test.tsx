import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, api } from "../../api";
import { setLocale } from "../../i18n";
import type { Grants, Interaction } from "../../types";
import { PermissionApproval } from "./approval";
import { PermissionsPanel } from "./permissions";
import { EnvironmentChoice } from "../composer/Composer";
import { emptyDraft } from "../composer/draft";
const grants: Grants = { version: "1", grants: [], effective_policy: [], execution_mode: "sandbox", sandbox: { available: false, state: "failed", platform: "win32", provider: "Windows" } };
beforeEach(() => setLocale("en"));
afterEach(() => { cleanup(); vi.restoreAllMocks(); setLocale("en"); });
const pickShellExecution = (label: RegExp) => {
  fireEvent.click(screen.getByRole("radio", { name: label }));
};
it("defaults to sandbox and chooses host independently of the worktree", () => {
  function Create() { const [draft, setDraft] = useState(emptyDraft()); return <EnvironmentChoice sessionId="new" draft={draft} git updateDraft={(_, patch) => setDraft(patch)} />; }
  render(<Create />);
  const execution = screen.getByRole("button", { name: "How shell commands run" });
  expect(execution.textContent).toContain("Sandbox (default)");
  fireEvent.click(execution);
  fireEvent.click(screen.getByRole("menuitemradio", { name: /Host \(no sandbox\)/ }));
  expect(screen.getByText(/without OS file or network isolation/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Where the session works" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: /Isolated execution/ }));
  expect(screen.getByRole("button", { name: "Where the session works" }).textContent).toContain("Isolated execution");
  expect(screen.getByRole("button", { name: "How shell commands run" }).textContent).toContain("Host (no sandbox)");
});
it("approves host Shell without sandbox setup or network toggle", async () => {
  const read = vi.spyOn(api, "grants").mockResolvedValue(grants);
  const respond = vi.fn().mockResolvedValue(true);
  const interaction: Interaction = { request_id: "host", kind: "permission", tool_name: "shell", execution_mode: "host", command: "echo hello", choices: [
    { id: "allow_once", label: "Once", scope: "", persistence: "" },
    { id: "allow_once_network", label: "Network", scope: "", persistence: "" },
    { id: "deny", label: "Deny", scope: "", persistence: "" },
  ] };
  render(<PermissionApproval sessionId="s" interaction={interaction} respond={respond} />);
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.queryByText("Initialize / retry (administrator)")).toBeNull();
  expect(screen.getByText(/without OS file or network isolation/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /Allow once/i }));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("host", "allow_once"));
  expect(read).not.toHaveBeenCalled();
});
it("switches explicitly through policy and refreshes server facts", async () => {
  const update = vi.fn(); const policy = vi.spyOn(api, "policy").mockResolvedValue({});
  vi.spyOn(api, "grants").mockResolvedValue({ ...grants, execution_mode: "host" });
  render(<PermissionsPanel sessionId="s" grants={grants} update={update} interactionMode="agent" permissionMode="default" />);
  expect(policy).not.toHaveBeenCalled();
  pickShellExecution(/Host \(no sandbox\)/);
  await waitFor(() => expect(policy).toHaveBeenCalledWith("s", { execution_mode: "host" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith(expect.objectContaining({ execution_mode: "host" })));
});
it("retains the mode on a 409 without retrying", async () => {
  const policy = vi.spyOn(api, "policy").mockRejectedValue(new ApiError("Stop background Shell before switching", 409));
  render(<PermissionsPanel sessionId="s" grants={grants} update={vi.fn()} interactionMode="agent" permissionMode="default" />);
  pickShellExecution(/Host \(no sandbox\)/);
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Stop background Shell before switching");
  expect(screen.getByRole("radio", { name: "Sandbox (default)" }).getAttribute("aria-checked")).toBe("true");
  expect(policy).toHaveBeenCalledTimes(1);
});
it("disables busy switching and localizes the host scope", () => {
  setLocale("zh-CN");
  render(<PermissionsPanel sessionId="s" grants={{ ...grants, execution_mode: "host" }} update={vi.fn()} executionBusy interactionMode="plan" permissionMode="default" />);
  expect(screen.getByRole("radio", { name: "宿主（无沙箱）" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("radio", { name: "沙箱（默认）" })).toHaveProperty("disabled", true);
  expect(screen.getByText(/Plan 和 Ask 仍禁止 Shell/)).toBeTruthy();
});

it("spells out mode caveats only for the broadest mode", () => {
  const { rerender } = render(<PermissionsPanel sessionId="s" grants={grants} update={vi.fn()} interactionMode="agent" permissionMode="default" />);
  expect(document.querySelector(".perm-mode-help")).toBeNull();
  expect(screen.getByRole("radio", { name: /Ask/ }).getAttribute("title")).toMatch(/ask first/);
  rerender(<PermissionsPanel sessionId="s" grants={{ ...grants, permission_mode: "bypass" }} update={vi.fn()} interactionMode="agent" permissionMode="bypass" />);
  expect(document.querySelector(".perm-mode-help")?.textContent).toBeTruthy();
});
