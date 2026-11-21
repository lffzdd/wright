import type { ReactNode } from "react";
import type { useT } from "../../../i18n";
import type { RuntimeSetting } from "../../../types";
import type { ThoughtStyle } from "../../../thoughtStyle";
import { SettingRow, type FieldContext } from "./fields";
import type { HooksState, McpConfigState, PermissionDefaultsState, SecretsState, SkillSwitchesState } from "./resources";
import type { RuntimeSettingsState } from "./useRuntimeSettings";

export type SectionId = "general" | "models" | "context" | "permissions" | "tools" | "memory" | "knowledge" | "advanced";
export const SECTIONS: SectionId[] = ["general", "models", "context", "permissions", "tools", "memory", "knowledge", "advanced"];

/**
 * One row of the page. ``group`` names the card it sits in; entries of one
 * group in a row share a card with that heading.
 */
export type Entry = { id: string; terms: string; node: ReactNode; group?: string };

export type PreferencePatch = {
  theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string; thought_style?: ThoughtStyle;
};

export type SectionContext = {
  tr: ReturnType<typeof useT>;
  projectId: string | null;
  searching: boolean;
  runtime: RuntimeSettingsState;
  field: FieldContext;
  secrets: SecretsState;
  permissions: PermissionDefaultsState;
  skills: SkillSwitchesState;
  mcp: McpConfigState;
  hooks: HooksState;
  /** Appearance preferences live in preferences.json, not in settings. */
  appearance: {
    theme: "dark" | "light"; thoughtStyle: ThoughtStyle; inspectorOpen: boolean; apply: (patch: PreferencePatch) => void;
  };
  showKeys: boolean;
  setShowKeys: (value: boolean) => void;
  /** A row for a typed setting, or null when the server does not know the key. */
  row: (key: string, extra?: Omit<Parameters<typeof SettingRow>[0], "item" | "ctx">, group?: string) => Entry | null;
};

/** Search text for one key: its label, hint, dotted name and variables. */
export function settingTerms(tr: ReturnType<typeof useT>, item: RuntimeSetting): string {
  const hintKey = `web.setting.${item.key}.hint`;
  return [tr(`web.setting.${item.key}`), tr(hintKey) === hintKey ? item.description : tr(hintKey), item.key, ...item.env].join(" ");
}

export function present(list: Array<Entry | null | false | undefined>): Entry[] {
  return list.filter((entry): entry is Entry => Boolean(entry));
}

/** A row that only explains, such as why the options below are hidden. */
export function note(id: string, text: string, group?: string): Entry {
  return { id, terms: "", group, node: <p className="hint settings-gated">{text}</p> };
}

/** Consecutive entries of one group, so each becomes one card. */
export function grouped(entries: Entry[]): Array<{ group: string; items: Entry[] }> {
  const groups: Array<{ group: string; items: Entry[] }> = [];
  for (const entry of entries) {
    const name = entry.group ?? "";
    const last = groups[groups.length - 1];
    if (last && last.group === name) last.items.push(entry);
    else groups.push({ group: name, items: [entry] });
  }
  return groups;
}
