import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ContextRing } from "./ContextRing";

afterEach(cleanup);

describe("ContextRing", () => {
  it("shows the share as a ring and keeps the exact figures in its label", () => {
    render(<ContextRing total={24_400} limit={128_000} />);
    const ring = screen.getByRole("meter");
    expect(ring.getAttribute("aria-valuenow")).toBe("19");
    expect(ring.getAttribute("aria-label")).toContain("24.4k / 128k");
    expect(screen.queryByText("19%")).toBeNull();
    expect(screen.getByRole("tooltip").textContent).toBe("Context 24.4k / 128k tokens");
    expect((ring.querySelector(".ring-fg") as SVGCircleElement).style.strokeDashoffset).toBe(String(94.25 * (1 - 0.19)));
  });

  it("shows the percentage once the context is filling up", () => {
    render(<ContextRing total={75} limit={100} />);
    expect(screen.getByText("75%")).toBeTruthy();
  });

  it("puts the restored usage line on the clickable control", () => {
    render(<ContextRing total={24_400} limit={128_000} onClick={() => undefined} />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Context 24.4k / 128k tokens");
    expect(screen.getByRole("tooltip").textContent).toBe("Context 24.4k / 128k tokens");
    expect(screen.getByRole("meter").getAttribute("aria-valuenow")).toBe("19");
  });

  it("escalates its tone as the context fills", () => {
    const { rerender } = render(<ContextRing total={10} limit={100} />);
    expect(document.querySelector(".ctx-ring")?.className).not.toMatch(/warn|full/);
    rerender(<ContextRing total={75} limit={100} />);
    expect(document.querySelector(".ctx-ring")?.className).toMatch(/warn/);
    rerender(<ContextRing total={95} limit={100} />);
    expect(document.querySelector(".ctx-ring")?.className).toMatch(/full/);
  });
});
