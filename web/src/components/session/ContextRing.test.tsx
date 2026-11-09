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
    expect(ring.textContent).toBe("19%");
  });

  it("escalates its tone as the context fills", () => {
    const { rerender } = render(<ContextRing total={10} limit={100} />);
    expect(screen.getByRole("meter").className).not.toMatch(/warn|full/);
    rerender(<ContextRing total={75} limit={100} />);
    expect(screen.getByRole("meter").className).toMatch(/warn/);
    rerender(<ContextRing total={95} limit={100} />);
    expect(screen.getByRole("meter").className).toMatch(/full/);
  });
});
