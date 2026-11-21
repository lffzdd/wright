import { useEffect, useRef, useState } from "react";
import { api } from "../../../api";
import { useT } from "../../../i18n";
import type { RuntimeSetting, RuntimeSettingsView, SettingScope } from "../../../types";
import type { Notice } from "../shared";

export type { Notice };

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
    environment: Array.isArray(candidate.environment) ? candidate.environment : [],
  };
}

/** Runs one save at a time and reports it in the page header. */
export type Run = <T>(work: () => Promise<T>) => Promise<T | undefined>;

export function useSaveStatus(): { notice: Notice; run: Run; report: (notice: Notice) => void } {
  const tr = useT();
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const generation = useRef(0);
  const run: Run = async (work) => {
    const request = ++generation.current;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try {
      const result = await work();
      if (request === generation.current) setNotice({ tone: "ok", text: tr("web.saved") });
      return result;
    } catch (error) {
      if (request === generation.current) setNotice({ tone: "error", text: error instanceof Error ? error.message : String(error) });
      return undefined;
    }
  };
  return { notice, run, report: setNotice };
}

export type RuntimeSettingsState = {
  view: RuntimeSettingsView | null;
  error: string;
  save: (key: string, value: unknown, scope: SettingScope) => Promise<void>;
  reload: () => void;
  item: (key: string) => RuntimeSetting | undefined;
};

/**
 * Typed runtime settings from ~/.wright/settings.json and the project's
 * .wright/settings.json. The server applies the layer rules; each row picks
 * the file it writes to.
 */
export function useRuntimeSettings(projectId: string | null, run: Run): RuntimeSettingsState {
  const [view, setView] = useState<RuntimeSettingsView | null>(null);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    const request = ++generation.current;
    api.runtimeSettings(projectId)
      .then((next) => { if (request === generation.current) { setView(asView(next)); setError(""); } })
      .catch((reason: unknown) => {
        if (request === generation.current) setError(reason instanceof Error ? reason.message : String(reason));
      });
  }, [projectId, nonce]);
  // A different project starts from a blank page rather than the last one's values.
  useEffect(() => { setView(null); }, [projectId]);

  const save = async (key: string, value: unknown, scope: SettingScope) => {
    const request = ++generation.current;
    const next = await run(() => api.setRuntimeSetting(projectId, { scope, key, value }));
    if (next && request === generation.current) setView(asView(next));
  };

  const item = (key: string) => view?.settings.find((entry) => entry.key === key);
  return { view, error, save, reload: () => setNonce((value) => value + 1), item };
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
