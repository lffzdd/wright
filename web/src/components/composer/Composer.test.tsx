import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { Composer } from "./Composer";
import { emptyDraft, type Draft } from "./draft";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function mount(prompt = "Hello") {
  const submit = vi.fn(async (..._args: unknown[]) => "accepted" as const);
  function Harness() {
    const [draft, setDraft] = useState<Draft>({ ...emptyDraft(), prompt });
    return <Composer sessionId="session-a" connection="connected" draft={draft}
      updateDraft={(_, patch) => setDraft(patch)} submit={submit} cancel={async () => undefined} running={false} />;
  }
  const rendered = render(<Harness />);
  return { ...rendered, submit, box: screen.getByRole("textbox", { name: "Message Wright" }) };
}

describe("composer input", () => {
  it("confirms Chinese IME composition without submitting, then submits on a separate Enter", async () => {
    const { box, submit } = mount("你好");
    fireEvent.compositionStart(box);
    fireEvent.keyDown(box, { key: "Enter", code: "Enter", isComposing: true });
    expect(submit).not.toHaveBeenCalled();
    fireEvent.compositionEnd(box);
    fireEvent.keyDown(box, { key: "Enter", code: "Enter" });
    await act(async () => undefined);
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit.mock.calls[0]?.[1]).toBe("你好");
  });

  it("does not select a mention when Enter is confirming an IME candidate", async () => {
    vi.spyOn(api, "search").mockResolvedValue({ kind: "file", results: [{ path: "example.txt" }] });
    const { box, submit } = mount("");
    fireEvent.change(box, { target: { value: "@文", selectionStart: 2 } });
    await screen.findByRole("option", { name: "example.txt" });
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(screen.getByRole("listbox")).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toBe("@文");
    expect(submit).not.toHaveBeenCalled();
  });

  it("does not submit while an attachment is still uploading", async () => {
    let finish!: (value: Awaited<ReturnType<typeof api.uploadAttachment>>) => void;
    vi.spyOn(api, "uploadAttachment").mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const { container, box, submit } = mount();
    const input = container.querySelector('input[accept="image/png,image/jpeg,image/webp"]')!;
    fireEvent.change(input, { target: { files: [new File(["image"], "example.png", { type: "image/png" })] } });
    fireEvent.submit(container.querySelector("form")!);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(submit).not.toHaveBeenCalled();
    await act(async () => { finish({ id: "a", filename: "example.png" } as Awaited<ReturnType<typeof api.uploadAttachment>>); });
    fireEvent.keyDown(box, { key: "Enter" });
    await act(async () => undefined);
    expect(submit).toHaveBeenCalledWith(expect.any(String), "Hello", ["a"], [], []);
  });

  it("never selects previous-query results while the new mention lookup is pending", async () => {
    let finish!: (value: Awaited<ReturnType<typeof api.search>>) => void;
    vi.spyOn(api, "search").mockResolvedValueOnce({ kind: "file", results: [{ path: "old.txt" }] })
      .mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const { box, submit } = mount("");
    fireEvent.change(box, { target: { value: "@old", selectionStart: 4 } });
    expect(await screen.findByRole("option", { name: "old.txt" })).toBeTruthy();
    fireEvent.change(box, { target: { value: "@new", selectionStart: 4 } });
    expect(screen.queryByRole("option", { name: "old.txt" })).toBeNull();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(submit).not.toHaveBeenCalled();
    expect((box as HTMLTextAreaElement).value).toBe("@new");
    await act(async () => { finish({ kind: "file", results: [{ path: "new.txt" }] }); });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(screen.getByText("new.txt")).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toBe("");
    expect(submit).not.toHaveBeenCalled();
  });

  it("lets Escape dismiss a mention lookup even before results arrive", async () => {
    vi.spyOn(api, "search").mockReturnValue(new Promise(() => undefined));
    const { box } = mount("");
    fireEvent.change(box, { target: { value: "@slow", selectionStart: 5 } });
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
