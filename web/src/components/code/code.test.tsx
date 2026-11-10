import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../../i18n";
import { ToolCard } from "../cards/cards";
import { CodeView } from "./CodeView";
import { languageForPath } from "./highlight";
import { OpenFileContext } from "./openFile";

beforeEach(() => setLocale("en"));
afterEach(() => { cleanup(); setLocale("en"); });

it("picks a grammar from the file name", () => {
  expect(languageForPath("src/app/main.py")).toBe("python");
  expect(languageForPath("web/src/App.tsx")).toBe("tsx");
  expect(languageForPath("Dockerfile")).toBe("dockerfile");
  expect(languageForPath("notes.unknown")).toBeUndefined();
  expect(languageForPath(undefined)).toBeUndefined();
});

it("shows each line number once and marks the focused line", () => {
  render(<CodeView lines={[{ num: 283, text: "a = 1" }, { num: 284, text: "b = 2" }]} focusLine={284} />);
  expect([...document.querySelectorAll(".cv-num")].map((node) => node.textContent)).toEqual(["283", "284"]);
  expect(document.querySelector(".cv-focus")?.textContent).toBe("284b = 2");
});

it("labels a read with its line range and opens the file at its first line", () => {
  const openFile = vi.fn();
  render(<OpenFileContext.Provider value={openFile}>
    <ToolCard name="read" phase="succeeded" args={{ file: "src/a.py" }} data={{ content: "283|x = 1\n284|y = 2\n", start_line: 283, end_line: 284 }} />
  </OpenFileContext.Provider>);
  expect(screen.getByText("L283–284")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  fireEvent.click(screen.getByRole("button", { name: /Open in Files/ }));
  expect(openFile).toHaveBeenCalledWith("src/a.py", 283);
  fireEvent.click(screen.getByText("y = 2"));
  expect(openFile).toHaveBeenLastCalledWith("src/a.py", 284);
});

it("opens a search hit's file at its line", () => {
  const openFile = vi.fn();
  render(<OpenFileContext.Provider value={openFile}>
    <ToolCard name="grep" phase="succeeded" args={{ pattern: "Busy" }} data={{ matches: [{ path: "a.py", line: 9, text: "raise Busy()" }] }} />
  </OpenFileContext.Provider>);
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  fireEvent.click(document.querySelector(".cv-row mark")!);
  expect(openFile).toHaveBeenCalledWith("a.py", 9);
  fireEvent.click(screen.getByRole("button", { name: "a.py" }));
  expect(openFile).toHaveBeenLastCalledWith("a.py");
});

it("shows a plan as a checklist and singles out the step an update changed", () => {
  const plan = { objective: "Ship the fix", steps: [
    { id: "step_1", title: "Check the rollback", status: "completed", note: "Rollback is right" },
    { id: "step_2", title: "Add a test", status: "in_progress", note: "" },
    { id: "step_3", title: "Run the suite", status: "pending", note: "" },
  ] };
  render(<ToolCard name="update_plan" phase="succeeded" args={{ step_id: "step_1", status: "completed" }} data={plan} />);
  expect(screen.getByText("1/3 done")).toBeTruthy();
  expect(screen.getAllByText("Check the rollback")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  const items = [...document.querySelectorAll(".plan-checklist li")];
  expect(items.map((item) => item.className)).toEqual(["completed focus", "in_progress dim", "pending dim"]);
  expect(screen.getByText("Rollback is right")).toBeTruthy();
  expect(screen.getByText("Ship the fix")).toBeTruthy();
});
