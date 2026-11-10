import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../../i18n";
import { InteractionCard, QuestionRecord, toolCardFromTimeline } from "./cards";

beforeEach(() => setLocale("en"));
afterEach(() => { cleanup(); setLocale("en"); });

const args = { question: "Which test next?", context: "Covers the options branch.", options: ["Replace branch", "Illegal transitions", "Both"] };

it("folds an answered question to its answer and opens to every option offered", () => {
  render(<QuestionRecord args={args} data={{ question: "Which test next?", answer: "Both" }} phase="succeeded" />);
  expect(screen.getByText("Which test next?")).toBeTruthy();
  expect(screen.getByText("Both")).toBeTruthy();
  expect(screen.queryByText("Replace branch")).toBeNull();
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  expect(screen.getByText("Replace branch")).toBeTruthy();
  expect(screen.getByText("Illegal transitions")).toBeTruthy();
  const chosen = screen.getAllByRole("listitem").filter((li) => li.getAttribute("aria-current") === "true");
  expect(chosen.map((li) => li.textContent)).toEqual(["3Both"]);
});

it("shows an answer typed instead of an option as the person's own", () => {
  render(<QuestionRecord args={args} data={{ answer: "Neither, ship it" }} phase="succeeded" />);
  fireEvent.click(screen.getByRole("button"));
  expect(screen.getByText("Your own answer")).toBeTruthy();
  expect(screen.getAllByText("Neither, ship it")).toHaveLength(2);
  expect(screen.getAllByRole("listitem").some((li) => li.getAttribute("aria-current") === "true")).toBe(false);
});

it("says a cancelled question got no answer and hides one still waiting", () => {
  const { container, rerender } = render(<QuestionRecord args={args} phase="running" />);
  expect(container.textContent).toBe("");
  rerender(<QuestionRecord args={args} phase="failed" />);
  expect(screen.getByText("No answer")).toBeTruthy();
});

it("renders an ask_user timeline item as a question record, not a tool card", () => {
  render(toolCardFromTimeline({ id: "q", kind: "tool", name: "ask_user", phase: "succeeded", arguments: args, result: { ok: true, data: { answer: "Both" } } }));
  expect(screen.queryByText("ask_user")).toBeNull();
  expect(screen.getByText("Both")).toBeTruthy();
});

it("answers an open question by number key", () => {
  const respond = vi.fn().mockResolvedValue(true);
  render(<InteractionCard sessionId="s" interaction={{ request_id: "r", kind: "ask_user", ...args }} respond={respond} />);
  fireEvent.keyDown(screen.getByRole("alert"), { key: "2" });
  expect(respond).toHaveBeenCalledWith("r", "Illegal transitions");
});
