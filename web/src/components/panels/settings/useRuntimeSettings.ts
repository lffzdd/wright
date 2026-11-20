import { useEffect, useRef, useState } from "react";
import { api } from "../../../api";
import { useT } from "../../../i18n";
import type { RuntimeSetting, RuntimeSettingsView, SettingScope } from "../../../types";

export type Notice = { tone: "idle" | "busy" | "ok" | "error"; text: string };

/** Accept only a well-formed view, so an old server or a stub cannot break the page. */
export function asView(value: unknown): RuntimeSettingsView | null {
  const candidate = value as Partial<RuntimeSettingsView> | null;
  if (!candidate || !Array.isArray(candidate.settings) || !candidate.paths || typeof candidate.paths.user !== "string") return null;
  const models = candidate.models;
  const wellFormed = models && Array.isArray(models.providers) && Array.isArray(models.models) && models.endpoint;
  return {
    paths: candidate.paths,
    settings: candidate.settings,
    warnings: Array.isArray(candidate.warnings) ? candidate.warnings : [],
    models: wellFormed ? models : undefined,
  };
}

export type RuntimeSettingsState = {
  view: RuntimeSettingsView | null;
  scope: SettingScope;
  setScope: (scope: SettingScope) => void;
  notice: Notice;
  save: (key: string, value: unknown) => Promise<void>;
  item: (key: string) => RuntimeSetting | undefined;
};

/**
 * Typed runtime settings from ~/.wright/settings.json and the project's
 * .wright/settings.json. The server applies the layer rules; the page edits
 * one file at a time and shows where each effective value came from.
 */
export function useRuntimeSettings(projectId: string | null): RuntimeSettingsState {
  const tr = useT();
  const [view, setView] = useState<RuntimeSettingsView | null>(null);
  const [scope, setScope] = useState<SettingScope>("user");
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const generation = useRef(0);

  useEffect(() => {
    const request = ++generation.current;
    setView(null);
    setScope("user");
    api.runtimeSettings(projectId)
      .then((next) => { if (request === generation.current) { setView(asView(next)); setNotice({ tone: "idle", text: "" }); } })
      .catch((error: unknown) => { if (request === generation.current) setNotice({ tone: "error", text: error instanceof Error ? error.message : String(error) }); });
  }, [projectId]);

  const save = async (key: string, value: unknown) => {
    const request = ++generation.current;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try {
      const next = await api.setRuntimeSetting(projectId, { scope, key, value });
      if (request !== generation.current) return;
      setView(asView(next));
      setNotice({ tone: "ok", text: tr("web.saved") });
    } catch (error) {
      if (request === generation.current) setNotice({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    }
  };

  const item = (key: string) => view?.settings.find((entry) => entry.key === key);
  return { view, scope, setScope, notice, save, item };
}

/** Text shown for a value in an input or a hint. */
export function display(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

export function fileValue(item: RuntimeSetting, scope: SettingScope): unknown {
  return scope === "user" ? item.user_value : item.project_value;
}
