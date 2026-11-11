import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../../i18n";
import { ToolCard, toolCardFromState, toolCardFromTimeline } from "../cards/cards";
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

const planDisplay = { objective: "Ship the fix", steps: [
  { id: "step_1", title: "Check the rollback", status: "completed", note: "" },
  { id: "step_2", title: "Add a test", status: "in_progress", note: "" },
] };
const planAck = "Plan updated: 1/2 steps done, step_2 in progress.";

function expectPlanChecklist() {
  expect(screen.getByText("1/2 done")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  expect(screen.queryByText(planAck)).toBeNull();
  expect([...document.querySelectorAll(".plan-checklist li")].map((item) => item.className)).toEqual(["completed", "in_progress"]);
}

it("renders a timeline plan card from the result's display field, not the short model-facing data", () => {
  render(toolCardFromTimeline({ id: "p", kind: "tool", name: "update_plan", phase: "succeeded", arguments: {}, result: { ok: true, data: planAck, display: planDisplay } }));
  expectPlanChecklist();
});

it("renders a live plan card from the finished event's display field", () => {
  render(toolCardFromState({ call_id: "p", name: "update_plan", phase: "succeeded", ok: true, data: planAck, display: planDisplay }));
  expectPlanChecklist();
});

it("shows a rejected plan update as the unchanged plan, not a red failure", () => {
  const err = "step_2 is in progress. Finish, block, or skip it before starting step_3.";
  const display = { objective: "Ship the fix", steps: [
    { id: "step_1", title: "Check the rollback", status: "completed", note: "" },
    { id: "step_2", title: "Add a test", status: "in_progress", note: "" },
    { id: "step_3", title: "Run the suite", status: "pending", note: "" },
  ], rejected: { code: "plan.step_busy", params: { active_id: "step_2", step_id: "step_3" } } };
  const { container } = render(toolCardFromTimeline({ id: "p", kind: "tool", name: "update_plan", phase: "failed",
    arguments: { steps: [{ id: "step_3", title: "Run the suite", status: "in_progress" }] }, result: { ok: false, err, data: {}, display } }));
  expect(screen.getByText("Not applied")).toBeTruthy();
  expect(screen.queryByText("failed")).toBeNull();
  expect(container.querySelector(".step-icon.failed")).toBeNull();
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  expect(container.querySelector(".tool-card-body.failed")).toBeNull();
  expect(screen.getByText(err)).toBeTruthy();
  const items = [...document.querySelectorAll(".plan-checklist li")];
  expect(items.map((item) => item.className)).toEqual(["completed dim", "in_progress blocker", "pending focus"]);
  expect(items[2].textContent).toContain("Tried: In progress");
});
