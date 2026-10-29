import { t } from "../i18n";
import type { PermissionResource } from "../types";

/** Quotes retain visible argument boundaries; this is a description, not executable shell. */
export function commandArguments(argv: string[]): string {
  return argv.map((arg) => /^[\w./:-]+$/.test(arg) ? arg : JSON.stringify(arg)).join(" ");
}

export function commandRuleLabel(resource: PermissionResource, fallback: string): string {
  const argv = resource.argv ?? [];
  if (resource.command_match === "executable") return t("web.grant_line.executable", { program: argv[0] || fallback });
  return t("web.grant_line.argv_prefix", { prefix: argv.length ? commandArguments(argv) : fallback });
}

export function commandRuleDetails(resource: PermissionResource): string {
  return t(resource.command_match === "executable" ? "web.approval.executable_details" : "web.approval.prefix_details");
}

export function networkRuleLabel(resource: PermissionResource, fallback: string, methods?: string[]): string {
  return t(resource.path_match === "exact" ? "web.grant_line.network_path" : "web.grant_line.network_origin", {
    target: fallback, methods: (resource.methods ?? methods)?.join(", ") || "GET",
  });
}
