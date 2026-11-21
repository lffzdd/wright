import { useEffect, useRef, useState } from "react";
import { api } from "../../../api";
import type {
  HooksView, McpConfigView, McpServerForm, PermissionDefaultsView, SettingScope, SkillSwitchesView,
} from "../../../types";
import type { Run } from "./useRuntimeSettings";

/** One server resource: loaded on mount and when `key` changes, replaced by each edit's response. */
export type Resource<T> = {
  data: T | null;
  error: string;
  apply: (edit: () => Promise<T>) => Promise<boolean>;
  reload: () => void;
};

export function messageOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function useResource<T>(load: () => Promise<T>, key: string, run: Run): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  const generation = useRef(0);
  // `load` is rebuilt every render; `key` names what it reads.
  const loader = useRef(load);
  loader.current = load;
  useEffect(() => {
    const request = ++generation.current;
    loader.current()
      .then((next) => { if (request === generation.current) { setData(next); setError(""); } })
      .catch((reason: unknown) => { if (request === generation.current) setError(messageOf(reason)); });
  }, [key, nonce]);
  const apply = async (edit: () => Promise<T>) => {
    const request = ++generation.current;
    const next = await run(edit);
    if (next === undefined) return false;
    if (request === generation.current) setData(next);
    return true;
  };
  return { data, error, apply, reload: () => setNonce((value) => value + 1) };
}

export function useSecrets(run: Run) {
  const resource = useResource(() => api.secrets(), "secrets", run);
  return {
    ...resource,
    set: (name: string, value: string | null) => resource.apply(() => api.setSecret(name, value)),
  };
}
export type SecretsState = ReturnType<typeof useSecrets>;

export function usePermissionDefaults(projectId: string | null, run: Run) {
  const resource = useResource<PermissionDefaultsView>(() => api.permissionDefaults(projectId), `permissions:${projectId}`, run);
  return {
    ...resource,
    setMode: (mode: string) => resource.apply(() => api.setPermissionMode(projectId, mode)),
    addRule: (body: { effect: "deny" | "ask"; scope: SettingScope; text: string }) => resource.apply(() => api.addPermissionRule(projectId, body)),
    removeRule: (scope: string, id: string) => resource.apply(() => api.removePermissionRule(projectId, scope, id)),
  };
}
export type PermissionDefaultsState = ReturnType<typeof usePermissionDefaults>;

export function useSkillSwitches(projectId: string | null, run: Run) {
  const resource = useResource<SkillSwitchesView>(() => api.skillSwitches(projectId), `skills:${projectId}`, run);
  return {
    ...resource,
    toggle: (id: string, enabled: boolean) => resource.apply(() => api.setSkillSwitch(projectId, id, enabled)),
  };
}
export type SkillSwitchesState = ReturnType<typeof useSkillSwitches>;

export function useMcpConfig(projectId: string | null, run: Run) {
  const resource = useResource<McpConfigView>(() => api.mcpConfig(projectId), `mcp:${projectId}`, run);
  return {
    ...resource,
    save: (name: string, form: McpServerForm) => resource.apply(() => api.saveMcpServer(projectId, name, form)),
    remove: (scope: SettingScope, name: string) => resource.apply(() => api.removeMcpServer(projectId, scope, name)),
  };
}
export type McpConfigState = ReturnType<typeof useMcpConfig>;

export function useHooks(run: Run) {
  return useResource<HooksView>(() => api.hooks(), "hooks", run);
}
export type HooksState = ReturnType<typeof useHooks>;
