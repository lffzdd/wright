import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { setLocale } from "../../i18n";
import type { Grants } from "../../types";
import { PolicyAuditDetail } from "./policyAuditDetail";

it("dedupes global denies and saved session allows in the audit panel", () => {
  setLocale("en");
  const base = (decision: "allow" | "ask" | "deny") => ({ decision, source: "default", reason_code: "", reason_params: {} });
  const grants: Grants = {
    version: "1",
    grants: [{ id: "a", source: "session", lifetime: "session", resource_kind: "shell", target: "pwd", operations: ["shell"], recursive: false, tool: "*" }],
    effective_policy: [{
      operation: "shell",
      defaults: { in_scope: base("ask"), outside_scope: base("ask") },
      directories: [],
      read_only: [],
      rules: [
        { effect: "deny", scope: "user", rule: {}, target: "rm *", resource_kind: "command", operations: ["shell"], tool: "*", description: "", conditional: true },
        { effect: "allow", scope: "session", rule: {}, target: "pwd", resource_kind: "shell", operations: ["shell"], tool: "*", description: "", conditional: false },
        { effect: "allow", scope: "session", rule: {}, target: "echo ok", resource_kind: "shell", operations: ["shell"], tool: "*", description: "", conditional: false },
      ],
      constraints: ["rule_precedence"],
      precedence: [],
    }],
  };
  render(<PolicyAuditDetail grants={grants} />);
  expect(screen.getByText(/global deny rule/)).toBeTruthy();
  expect(screen.getByText(/session rule/)).toBeTruthy();
  expect(screen.queryByText("rm *")).toBeNull();
  expect(screen.queryByText("pwd")).toBeNull();
  expect(screen.getByText("echo ok")).toBeTruthy();
  expect(screen.getByText("Evaluation order")).toBeTruthy();
  expect(screen.getByText("Mode ceilings apply first; deny and ask rules override automatic allows.")).toBeTruthy();
});
