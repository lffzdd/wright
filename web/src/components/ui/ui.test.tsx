import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { setLocale } from "../../i18n";
import { AddWorkspaceDialog } from "../session/AddWorkspaceDialog";
import { SessionRail } from "../session/SessionRail";
import { confirmDialog, DialogHost, promptDialog } from "./dialogs";
import { MenuButton } from "./Menu";

beforeEach(() => setLocale("en"));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("in-app dialogs", () => {
  it("resolves a confirm from the app dialog instead of the browser", async () => {
    const native = vi.spyOn(window, "confirm");
    render(<DialogHost />);
    let answer: Promise<boolean> = Promise.resolve(false);
    act(() => { answer = confirmDialog({ title: "Delete it?", message: "Gone for good.", danger: true }); });
    expect(screen.getByRole("alertdialog", { name: "Delete it?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await expect(answer).resolves.toBe(true);
    expect(native).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("treats Escape as cancel and returns typed text from a prompt", async () => {
    render(<DialogHost />);
    let confirmed: Promise<boolean> = Promise.resolve(true);
    act(() => { confirmed = confirmDialog({ title: "Sure?" }); });
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Escape" });
    await expect(confirmed).resolves.toBe(false);

    let typed: Promise<string | null> = Promise.resolve(null);
    act(() => { typed = promptDialog({ title: "Name", initial: "old" }); });
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "new" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Name" }));
    await expect(typed).resolves.toBe("new");
  });
});

describe("MenuButton", () => {
  it("opens, runs an item, and closes on Escape", () => {
    const onSelect = vi.fn();
    render(<MenuButton label="More" items={[{ label: "Rename", onSelect }, "separator", { label: "Delete", danger: true, onSelect: vi.fn() }]}>…</MenuButton>);
    const trigger = screen.getByRole("button", { name: "More" });
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("SessionRail rows", () => {
  const sessions = [{ session_id: "5a3312aa9f", user_goal: "Fix login", project_id: "one", active: true, status: "idle", agent_status: "completed" }];

  it("shows titles without session IDs and offers a right-click menu", () => {
    const onRenameStart = vi.fn();
    render(<SessionRail sessions={sessions} selected={null} onSelect={vi.fn()} onCreate={vi.fn()} open workspaces={[{ project_id: "one", name: "One", selected: true }]} onRenameStart={onRenameStart} onArchive={vi.fn()} />);
    expect(screen.queryByText(/#5a3312/)).toBeNull();
    expect(screen.queryByText("Completed")).toBeNull();
    fireEvent.contextMenu(screen.getByRole("button", { name: /Fix login/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(onRenameStart).toHaveBeenCalledWith(sessions[0]);
  });

  it("renames in place and adds workspaces from a visible button", () => {
    const onRenameSubmit = vi.fn();
    const onRegisterWorkspace = vi.fn();
    render(<SessionRail sessions={sessions} selected={null} onSelect={vi.fn()} onCreate={vi.fn()} open workspaces={[{ project_id: "one", name: "One", selected: true }]} renaming="5a3312aa9f" onRenameSubmit={onRenameSubmit} onRegisterWorkspace={onRegisterWorkspace} />);
    const input = screen.getByRole("textbox", { name: "Session title" });
    fireEvent.change(input, { target: { value: "Fix OAuth login" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRenameSubmit).toHaveBeenCalledWith(sessions[0], "Fix OAuth login");
    fireEvent.click(screen.getByRole("button", { name: "Add workspace" }));
    expect(onRegisterWorkspace).toHaveBeenCalledOnce();
  });
});

describe("AddWorkspaceDialog", () => {
  it("browses folders and registers the current one", async () => {
    const folders = vi.spyOn(api, "folders").mockImplementation(async (path?: string) => path === "/home/me/code"
      ? { path: "/home/me/code", parent: "/home/me", home: "/home/me", git: true, entries: [], truncated: false }
      : { path: "/home/me", parent: "/home", home: "/home/me", git: false, entries: [{ name: "code", path: "/home/me/code", git: true }], truncated: false });
    const register = vi.fn().mockResolvedValue(undefined);
    const close = vi.fn();
    render(<AddWorkspaceDialog close={close} register={register} />);
    fireEvent.click(await screen.findByRole("button", { name: /code/ }));
    await waitFor(() => expect(folders).toHaveBeenCalledWith("/home/me/code"));
    await screen.findByText("No subfolders");
    fireEvent.click(screen.getByRole("button", { name: "Add this folder" }));
    await waitFor(() => expect(register).toHaveBeenCalledWith("/home/me/code"));
    await waitFor(() => expect(close).toHaveBeenCalled());
  });
});
