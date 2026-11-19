import { ArrowsClockwise, BookOpen, Brain, ChatCircleDots, Eye, FilePlus, FolderSimple, Globe, ListChecks, MagnifyingGlass, PencilSimple, TerminalWindow, Wrench, type Icon as PhosphorIcon } from "@phosphor-icons/react";

/** Display titles aligned with backend tool names where possible. */
const REFERENCE_TOOL_LABELS: Record<string, string> = {
  read: "Read", read_file: "Read",
  grep: "Grep", ripgrep: "Grep",
  glob: "Glob", glob_file_search: "Glob",
  write: "Write", write_file: "Write",
  edit: "Edit", edit_file: "Edit", apply_patch: "Edit", search_replace: "Edit",
  shell: "Shell", bash: "Shell", run_command: "Shell", execute_command: "Shell",
  ls: "List", list_dir: "List", list_directory: "List",
};

/** "web_fetch" -> "WebFetch": the tool's name as a short title. */
export function toolLabel(name: string): string {
  const key = name.toLowerCase();
  if (REFERENCE_TOOL_LABELS[key]) return REFERENCE_TOOL_LABELS[key];
  if (/^read[_-]/.test(key)) return "Read";
  if (/^write[_-]/.test(key)) return "Write";
  if (/glob/.test(key)) return "Glob";
  if (/grep/.test(key)) return "Grep";
  return name.split(/[_\-\s]+/).filter(Boolean).map((part) => part[0].toUpperCase() + part.slice(1)).join("");
}

/** Directory-listing tools: `ls`, plus the legacy `list_directory` in older sessions. */
export function isDirectoryTool(name: string): boolean {
  return name.toLowerCase() === "ls" || /directory/i.test(name);
}

/** Reference `TOOLS[].compact` + default-open kind (`read` | `grep` | `glob` | `edit` | `write` | `shell`). */
export function referenceToolKind(name: string, kind?: string): string {
  const key = name.toLowerCase();
  const family = toolFamily(name, kind);
  if (/^write/.test(key) || key === "write_file") return "write";
  if (family === "edit") return "edit";
  if (family === "shell") return "shell";
  if (/glob/.test(key)) return "glob";
  if (/grep/.test(key) || (family === "read" && /search/.test(key))) return "grep";
  if (family === "read" && (key.startsWith("read") || isDirectoryTool(key))) return isDirectoryTool(key) && !/glob/.test(key) ? "glob" : "read";
  if (isDirectoryTool(key)) return "glob";
  return "tool";
}

export function referenceToolCompact(kind: string): boolean {
  return kind === "read" || kind === "grep" || kind === "glob";
}

export function toolFamily(name: string, kind?: string): "shell" | "edit" | "read" | "tool" {
  const normalized = name.toLowerCase();
  if (kind === "shell" || normalized === "execute_command" || normalized === "shell") return "shell";
  if (kind === "edit" || ["write", "edit", "write_file", "edit_file", "search_replace", "apply_patch", "strreplace"].includes(normalized)) return "edit";
  if (kind === "tool" && (normalized.startsWith("read") || isDirectoryTool(normalized) || normalized === "grep")) return "read";
  if (normalized.startsWith("read") || isDirectoryTool(normalized)) return "read";
  return kind === "edit" ? "edit" : kind === "shell" ? "shell" : "tool";
}

export type StepCategory = "directories" | "reads" | "searches" | "web" | "commands" | "edits" | "memory" | "plans" | "tools";

/** What kind of work a tool call does, for its colour and the group summary. */
export function toolCategory(name: string, kind?: string): StepCategory {
  const family = toolFamily(name, kind);
  if (family === "shell") return "commands";
  if (family === "edit") return "edits";
  const lowered = name.toLowerCase();
  if (lowered.startsWith("web_") || lowered.startsWith("http")) return "web";
  // Follow-ups on a background command: shell_output, shell_kill, list_commands (and older names).
  if (lowered.startsWith("shell_") || /_commands?$/.test(lowered)) return "commands";
  // Memory and knowledge lookups are recall, not code search, even when named "search_*".
  if (/memory|episode|knowledge|commitment/.test(lowered)) return "memory";
  if (lowered.includes("plan")) return "plans";
  if (isDirectoryTool(lowered)) return "directories";
  if (/grep|glob|search/.test(lowered)) return "searches";
  if (lowered.startsWith("read")) return "reads";
  return "tools";
}

export function toolIcon(name: string, category: StepCategory, refKind?: string): PhosphorIcon {
  const lowered = name.toLowerCase();
  if (refKind === "write") return FilePlus;
  if (refKind === "edit") return PencilSimple;
  if (refKind === "shell") return TerminalWindow;
  if (refKind === "read") return Eye;
  if (refKind === "grep") return MagnifyingGlass;
  if (refKind === "glob") return FolderSimple;
  switch (category) {
    case "commands": return TerminalWindow;
    case "edits": return /^write/.test(lowered) ? FilePlus : PencilSimple;
    case "web": return Globe;
    case "memory": return lowered.includes("knowledge") ? BookOpen : Brain;
    case "plans": return ListChecks;
    case "directories": return FolderSimple;
    case "searches": return /glob/.test(lowered) ? FolderSimple : MagnifyingGlass;
    case "reads": return Eye;
    default:
      if (lowered === "ask_user") return ChatCircleDots;
      if (lowered.includes("loop")) return ArrowsClockwise;
      return Wrench;
  }
}
